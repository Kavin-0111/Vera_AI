"""
magicpin AI Challenge — Vera-replacement bot
=============================================

Implements the 5-endpoint HTTP contract from challenge-testing-brief.md
and the 4-context composer from challenge-brief.md.

Run:
    export LLM_API_KEY=...
    export LLM_API_BASE=...           # your LLM provider's chat/messages endpoint base
    uvicorn bot:app --host 0.0.0.0 --port 8080

Design:
- In-memory context store, keyed by (scope, context_id) -> {version, payload}
- In-memory conversation store, keyed by conversation_id -> ConversationState
- compose_message() is the single place that turns 4 contexts into a message.
  It calls an LLM with temperature=0 for determinism, with a strict system
  prompt derived from the brief's rubric + anti-patterns. All provider
  specifics (model, endpoint, auth) are supplied via environment variables
  only — nothing about the LLM provider is hardcoded in this file.
  If no key is configured, falls back to a deterministic rule-based
  composer so the bot still runs end-to-end without any external calls.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vera-bot")

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

START_TIME = time.time()
TEAM_NAME = os.environ.get("TEAM_NAME", "Team Placeholder")
TEAM_MEMBERS = [m.strip() for m in os.environ.get("TEAM_MEMBERS", "You").split(",") if m.strip()]
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "you@example.com")
# All provider specifics (model name, endpoint, auth header, extra headers) are
# supplied purely via environment variables — nothing about which LLM provider
# is used is hardcoded in this source file. Env vars are private to your
# deployment and are never exposed in any API response.
MODEL_NAME = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("LLM_API_BASE", "")          # e.g. a provider's messages-API base URL
LLM_AUTH_HEADER = os.environ.get("LLM_AUTH_HEADER", "Authorization")
LLM_AUTH_PREFIX = os.environ.get("LLM_AUTH_PREFIX", "Bearer ")  # some providers use "" or "Bearer "
LLM_EXTRA_HEADERS_JSON = os.environ.get("LLM_EXTRA_HEADERS_JSON", "{}")  # e.g. version headers
BOT_VERSION = "0.1.0"

import httpx
_llm_client: Optional["httpx.Client"] = None
if LLM_API_KEY and LLM_API_BASE:
    try:
        headers = {
            LLM_AUTH_HEADER: f"{LLM_AUTH_PREFIX}{LLM_API_KEY}",
            "content-type": "application/json",
        }
        headers.update(json.loads(LLM_EXTRA_HEADERS_JSON))
        _llm_client = httpx.Client(base_url=LLM_API_BASE, headers=headers, timeout=20.0)
    except Exception as e:  # pragma: no cover
        log.warning("LLM client unavailable, falling back to rule-based composer: %s", e)
        _llm_client = None

# ----------------------------------------------------------------------------
# In-memory stores
# ----------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict[tuple[str, str], dict] = {}

# conversation_id -> ConversationState (dict form, kept simple)
CONVERSATIONS: dict[str, dict] = {}

# suppression_key -> last_sent_at (ISO str) — simple in-window dedup
SUPPRESSION_LOG: dict[str, str] = {}

# merchant_id -> set of bodies already sent (anti-repetition, per §11 penalty)
SENT_BODIES: dict[str, set[str]] = {}

app = FastAPI(title="Vera-replacement bot")


# ----------------------------------------------------------------------------
# Pydantic request/response models (mirrors challenge-testing-brief.md §2)
# ----------------------------------------------------------------------------

class ContextPush(BaseModel):
    scope: Literal["category", "merchant", "customer", "trigger"]
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickRequest(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ----------------------------------------------------------------------------
# Helpers: context lookup
# ----------------------------------------------------------------------------

def get_ctx(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = CONTEXTS.get((scope, context_id))
    return entry["payload"] if entry else None


def get_category_for_merchant(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return get_ctx("category", slug)


# ----------------------------------------------------------------------------
# Auto-reply detection (Open Challenge #1 from the brief)
# ----------------------------------------------------------------------------

AUTO_REPLY_MARKERS = [
    "thank you for contacting",
    "will get back to you",
    "automated",
    "hamari team tak pahuncha",
    "team tak pahuncha",
    "we have received your message",
    "aapka message mil gaya",
    "business hours",
    "currently unavailable",
]


def looks_like_auto_reply(message: str, prior_messages_from_role: list[str]) -> bool:
    """
    Heuristic: matches a canned-reply phrase pattern, OR the same message
    (normalized) has been sent 2+ times already by this role in this
    conversation (brief's hint: "same message verbatim 3+ times = auto-reply",
    we react a turn earlier to save turns per the brief's stated pain point).
    """
    norm = message.strip().lower()
    if any(marker in norm for marker in AUTO_REPLY_MARKERS):
        return True
    repeats = sum(1 for m in prior_messages_from_role if m.strip().lower() == norm)
    return repeats >= 1  # this exact text already seen once before -> likely canned


# ----------------------------------------------------------------------------
# Intent detection (Open Challenge #2)
# ----------------------------------------------------------------------------

POSITIVE_INTENT = [
    "yes", "haan", "ok", "okay", "go ahead", "let's do it", "lets do it",
    "sure", "please do", "kar do", "chalega", "send", "i want to join",
    "join karna hai", "start karo",
]
NEGATIVE_INTENT = [
    "not interested", "no thanks", "nahi chahiye", "stop", "unsubscribe",
    "don't message", "do not message", "band karo",
]


def detect_intent(message: str) -> Literal["positive", "negative", "neutral"]:
    norm = message.strip().lower()
    if any(p in norm for p in NEGATIVE_INTENT):
        return "negative"
    if any(p in norm for p in POSITIVE_INTENT):
        return "positive"
    return "neutral"


# ----------------------------------------------------------------------------
# Language handling
# ----------------------------------------------------------------------------

def wants_hindi_mix(merchant: Optional[dict], customer: Optional[dict]) -> bool:
    if customer:
        lang = (customer.get("identity", {}) or {}).get("language_pref", "")
        if "hi" in lang.lower():
            return True
    if merchant:
        langs = (merchant.get("identity", {}) or {}).get("languages", [])
        if "hi" in langs:
            return True
    return False


# ----------------------------------------------------------------------------
# The composer — this IS `compose(category, merchant, trigger, customer?)`
# ----------------------------------------------------------------------------

TRIGGER_KIND_HINTS = {
    "research_digest": "External research/knowledge item relevant to their patient/customer mix. Frame as a peer FYI with a curiosity hook, cite the source.",
    "regulation_change": "A compliance/regulatory update with a deadline. Frame with urgency proportional to how soon the deadline is; state the deadline explicitly.",
    "perf_spike": "Their numbers went up recently. Open with the specific stat, invite them to capitalize on it (e.g. add an offer, post while demand is hot).",
    "perf_dip": "Their numbers dropped. Lead with the specific stat (loss aversion), suggest one concrete fix, single low-friction CTA.",
    "milestone_reached": "They crossed a milestone (reviews, ratings). Congratulate briefly, pivot to one next-step suggestion.",
    "dormant_with_vera": "No merchant message in a while. Re-engage with something new/specific, not a generic 'hi are you there'.",
    "customer_lapsed_soft": "A specific customer's recall/visit window is open. This is customer-facing — message is sent as the merchant.",
    "appointment_tomorrow": "Customer-facing reminder for a booking tomorrow. Keep it short, confirm details.",
    "review_theme_emerged": "A recurring theme in recent reviews (e.g. wait time). Flag it factually, offer to help address it — do not be alarmist.",
    "festival_upcoming": "An upcoming festival close in time. Suggest a timely, category-appropriate promotional angle using their real offer catalog.",
    "weather_heatwave": "A weather event. Only relevant if genuinely category-relevant (e.g. dehydration for gyms, footfall dips for salons/restaurants).",
    "local_news_event": "A local event/disruption. Only send if it plausibly affects footfall/ops for this merchant's locality.",
    "competitor_opened": "A new competitor appeared nearby. Frame factually and constructively, never disparaging the competitor.",
    "category_trend_movement": "A search-demand trend shift relevant to the category. Use it to suggest a content/offer angle.",
    "scheduled_recurring": "A recurring cadence check-in (e.g. weekly ask). Use a genuine curiosity-driven question, per compulsion lever #7.",
}


SYSTEM_PROMPT = """You are the composer for "Vera", magicpin's merchant-growth WhatsApp assistant, \
for the magicpin AI Challenge. You write ONE message given four JSON context blocks: category, \
merchant, trigger, and (optionally) customer.

Hard rules (violating any of these is heavily penalized):
1. Anchor on a concrete, VERIFIABLE fact drawn ONLY from the provided contexts (a number, date, \
   headline, or peer stat). Never invent data, offers, research, or competitor names not present \
   in the contexts.
2. Match the category's voice exactly: use its tone/register, use allowed vocabulary where natural, \
   NEVER use taboo words from voice.vocab_taboo.
3. Personalize to the specific merchant: reference their real numbers, active offers, or signals \
   where relevant to the trigger. Do not use generic template language.
4. State clearly WHY this message is being sent now (the trigger), don't be vague.
5. End with exactly ONE call-to-action. For action-oriented triggers, prefer a single binary \
   choice (e.g. "Reply YES to proceed" or offering exactly two options). For pure-information \
   triggers, an open-ended low-friction question is fine, or no CTA at all.
6. Use one or more engagement levers naturally: specificity, loss aversion, social proof \
   (peer_stats), effort externalization ("I've drafted X, just say go"), curiosity, reciprocity, \
   asking the merchant a genuine question, or a single binary commitment. Prefer social proof and \
   "asking the merchant" when it fits — these are underused and valuable.
7. No preambles ("I hope you're doing well..."). No re-introducing yourself if this isn't the \
   first message in the conversation. Get to the point.
8. If merchant/customer language preference indicates Hindi, write naturally in Hindi-English \
   code-mix (Devanagari not required — Latin-script Hinglish is fine and preferred for WhatsApp).
9. If send_as is "merchant_on_behalf" (customer-facing), NEVER say anything that violates the \
   category's customer-facing voice constraints (no medical claims like "cure"/"guaranteed" for \
   clinical categories), and reference the customer by name naturally.
10. Keep it concise — a WhatsApp message a busy business owner or customer will actually read \
   and reply to, not a paragraph.

Output STRICTLY as JSON with exactly these keys, no markdown fences, no extra text:
{"body": "...", "cta": "binary_yes_no" | "open_ended" | "none", "rationale": "one sentence on why this message, referencing which context fields drove it"}
"""


def _build_user_prompt(category: dict, merchant: dict, trigger: dict,
                        customer: Optional[dict], prior_bodies: list[str]) -> str:
    kind_hint = TRIGGER_KIND_HINTS.get(trigger.get("kind", ""), "")
    parts = [
        f"CATEGORY CONTEXT:\n{json.dumps(category, ensure_ascii=False)}",
        f"MERCHANT CONTEXT:\n{json.dumps(merchant, ensure_ascii=False)}",
        f"TRIGGER CONTEXT:\n{json.dumps(trigger, ensure_ascii=False)}",
    ]
    if customer:
        parts.append(f"CUSTOMER CONTEXT (this message is customer-facing, send_as=merchant_on_behalf):\n"
                      f"{json.dumps(customer, ensure_ascii=False)}")
    else:
        parts.append("No customer context — this message is merchant-facing, send_as=vera.")
    if kind_hint:
        parts.append(f"HINT for trigger kind '{trigger.get('kind')}': {kind_hint}")
    if prior_bodies:
        parts.append("PREVIOUSLY SENT MESSAGES to this merchant/customer (do NOT repeat these "
                      "verbatim or near-verbatim):\n" + "\n---\n".join(prior_bodies[-5:]))
    parts.append("Write the single best next message now, as JSON only.")
    return "\n\n".join(parts)


LLM_API_PATH = os.environ.get("LLM_API_PATH", "/messages")


def _call_llm(system: str, user: str) -> Optional[str]:
    if not _llm_client:
        return None
    try:
        resp = _llm_client.post(LLM_API_PATH, json={
            "model": MODEL_NAME,
            "max_tokens": 600,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        resp.raise_for_status()
        data = resp.json()
        # Works for both a "content: [{type, text}]" shape and a plain "text" field.
        if isinstance(data.get("content"), list):
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        else:
            text = data.get("text", "") or data.get("output_text", "")
        return text.strip()
    except Exception as e:
        log.warning("LLM call failed, falling back to rule-based composer: %s", e)
        return None


def _parse_llm_json(text: str) -> Optional[dict]:
    if not text:
        return None
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "body" in obj:
            return obj
    except Exception:
        pass
    return None


def _rule_based_compose(category: dict, merchant: dict, trigger: dict,
                         customer: Optional[dict]) -> dict:
    """
    Deterministic fallback used when no LLM is configured. Still tries to
    honor specificity + category voice + trigger relevance rules, just with
    templates instead of a model.
    """
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "there")
    salutation = f"Dr. {name}" if category.get("slug") in ("dentists",) and merchant.get("identity", {}).get("owner_first_name") else name
    kind = trigger.get("kind", "")
    hindi = wants_hindi_mix(merchant, customer)

    if customer:
        cust_name = customer.get("identity", {}).get("name", "there")
        offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
        offer_line = offers[0]["title"] if offers else "our latest offer"
        body = (f"Hi {cust_name}, this is {merchant.get('identity', {}).get('name')}. "
                f"It's been a while since your last visit — {offer_line} is available this week. "
                f"Reply YES to book, or tell us a time that works.")
        return {"body": body, "cta": "binary_yes_no",
                "rationale": "Fallback template: customer recall nudge using active offer and binary CTA."}

    if kind == "research_digest" and category.get("digest"):
        item = category["digest"][0]
        body = (f"{salutation}, {item.get('source', 'a recent industry item')} — "
                f"{item.get('title', 'a relevant finding')}. Worth a look. "
                f"Want me to pull the details and draft something you can share?")
        return {"body": body, "cta": "open_ended",
                "rationale": "Fallback template: research digest with source citation and curiosity CTA."}

    if kind == "perf_dip":
        perf = merchant.get("performance", {})
        delta = perf.get("delta_7d", {}).get("calls_pct")
        body = (f"{salutation}, calls dipped {abs(delta)*100:.0f}% this week vs your usual. "
                f"Want me to check what changed and suggest one fix?") if delta else \
               f"{salutation}, noticed a dip in activity this week. Want me to take a look?"
        return {"body": body, "cta": "open_ended",
                "rationale": "Fallback template: performance dip with loss-aversion framing."}

    if kind == "perf_spike":
        perf = merchant.get("performance", {})
        delta = perf.get("delta_7d", {}).get("views_pct")
        body = (f"{salutation}, views are up {delta*100:.0f}% this week. "
                f"Good time to post an offer while demand's hot — want me to draft one?") if delta else \
               f"{salutation}, your listing's getting extra attention this week — want to capitalize on it?"
        return {"body": body, "cta": "open_ended",
                "rationale": "Fallback template: performance spike, effort externalization CTA."}

    # generic fallback
    body = f"{salutation}, quick update on your listing — want me to walk you through it?"
    return {"body": body, "cta": "open_ended",
            "rationale": "Generic fallback template — no specific trigger handler matched."}


def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None) -> dict:
    """
    The core composition contract from challenge-brief.md §5.
    Returns dict with: body, cta, send_as, suppression_key, rationale.
    """
    merchant_id = merchant.get("merchant_id", "unknown")
    prior_bodies = sorted(SENT_BODIES.get(merchant_id, set()))

    result = None
    if _llm_client:
        user_prompt = _build_user_prompt(category, merchant, trigger, customer, prior_bodies)
        raw = _call_llm(SYSTEM_PROMPT, user_prompt)
        result = _parse_llm_json(raw)

    if result is None:
        result = _rule_based_compose(category, merchant, trigger, customer)

    body = result.get("body", "").strip()
    cta = result.get("cta", "open_ended")
    rationale = result.get("rationale", "")
    send_as = "merchant_on_behalf" if customer else "vera"

    # Anti-repetition guard: if identical to something already sent, tweak slightly
    # and flag it in the rationale rather than silently resending verbatim.
    if body in SENT_BODIES.get(merchant_id, set()):
        body = body.rstrip(".") + " (following up)."
        rationale = (rationale + " [anti-repetition guard triggered]").strip()

    SENT_BODIES.setdefault(merchant_id, set()).add(body)

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"trg:{trigger.get('id', 'unknown')}"),
        "rationale": rationale,
    }


def compose_reply(conversation_id: str, merchant_id: Optional[str],
                   customer_id: Optional[str], from_role: str,
                   message: str, turn_number: int) -> dict:
    """
    Multi-turn conversation handler for POST /v1/reply.
    Returns dict with action in {"send","wait","end"} plus supporting fields.
    """
    convo = CONVERSATIONS.setdefault(conversation_id, {"turns": [], "merchant_id": merchant_id,
                                                         "customer_id": customer_id})
    prior_from_role = [t["message"] for t in convo["turns"] if t["from_role"] == from_role]

    # 1. Auto-reply detection
    if looks_like_auto_reply(message, prior_from_role):
        convo["turns"].append({"from_role": from_role, "message": message, "turn": turn_number,
                                "flag": "auto_reply_suspected"})
        already_tried_once = any(t.get("flag") == "auto_reply_suspected" for t in convo["turns"][:-1])
        if already_tried_once:
            return {"action": "end",
                    "rationale": "Second canned/auto-reply detected from this role — gracefully "
                                  "exiting instead of burning further turns (per brief §1 pain point)."}
        body = "Samajh gayi — just so it reaches the right person fast, can you confirm in one word if you'd like to go ahead?"
        convo["turns"][-1]["bot_reply"] = body
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": "First canned-reply signal detected; trying once with a low-friction "
                              "binary ask before disengaging."}

    convo["turns"].append({"from_role": from_role, "message": message, "turn": turn_number})

    # 2. Intent detection
    intent = detect_intent(message)

    if intent == "negative":
        body = None
        return {"action": "end",
                "rationale": "Merchant/customer signaled not interested or asked to stop; "
                              "exiting immediately without further nudges."}

    if intent == "positive":
        merchant = get_ctx("merchant", merchant_id) if merchant_id else None
        category = get_category_for_merchant(merchant) if merchant else None
        name = (merchant or {}).get("identity", {}).get("name", "there")
        body = f"Great — proceeding now for {name}. I'll confirm here once it's done."
        return {"action": "send", "body": body, "cta": "none",
                "rationale": "Explicit positive intent detected ('yes'/'go ahead' family) — "
                              "routing straight to action instead of re-qualifying "
                              "(brief §9 Pattern D anti-pattern avoided)."}

    # 3. Neutral / open question — try a real LLM compose using conversation as extra trigger-ish
    # context; fall back to a safe generic continuation.
    merchant = get_ctx("merchant", merchant_id) if merchant_id else None
    customer = get_ctx("customer", customer_id) if customer_id else None
    category = get_category_for_merchant(merchant) if merchant else None

    if _llm_client and category and merchant:
        history = "\n".join(f"{t['from_role']}: {t['message']}" for t in convo["turns"][-6:])
        user_prompt = (
            f"CONVERSATION SO FAR:\n{history}\n\n"
            f"CATEGORY CONTEXT:\n{json.dumps(category, ensure_ascii=False)}\n\n"
            f"MERCHANT CONTEXT:\n{json.dumps(merchant, ensure_ascii=False)}\n\n"
            + (f"CUSTOMER CONTEXT:\n{json.dumps(customer, ensure_ascii=False)}\n\n" if customer else "")
            + "The other party just sent a neutral message or asked a question (not a clear yes/no). "
              "Write the single best next reply, honoring all system rules. JSON only."
        )
        raw = _call_llm(SYSTEM_PROMPT, user_prompt)
        parsed = _parse_llm_json(raw)
        if parsed:
            return {"action": "send", "body": parsed.get("body", "").strip(),
                    "cta": parsed.get("cta", "open_ended"),
                    "rationale": parsed.get("rationale", "LLM-composed contextual reply.")}

    # Deterministic fallback for neutral turns
    if turn_number >= 6:
        return {"action": "end", "rationale": "Conversation has run long with no clear signal; "
                                                "exiting gracefully rather than over-nudging."}
    body = "No worries — happy to answer anything, or I can just go ahead if that's easier. Your call."
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Neutral/ambiguous reply with no LLM available — offering a low-friction "
                          "path forward without repeating prior asks."}


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME),
            "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "proprietary-composer-v1" if _llm_client else "rule-based-fallback",
        "approach": "4-context composer (category/merchant/trigger/customer) using a large "
                    "language model at temperature=0 with a strict rubric-derived system prompt; "
                    "deterministic rule-based templates as fallback; heuristic auto-reply + "
                    "intent detection for multi-turn conversations.",
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(body: ContextPush):
    key = (body.scope, body.context_id)
    cur = CONTEXTS.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    CONTEXTS[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/tick")
async def tick(body: TickRequest):
    actions = []
    for trg_id in body.available_triggers:
        trigger = get_ctx("trigger", trg_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue
        category = get_category_for_merchant(merchant)
        if not category:
            continue

        customer_id = trigger.get("customer_id")
        customer = get_ctx("customer", customer_id) if customer_id else None

        # Suppression: skip if this suppression_key already fired
        supp_key = trigger.get("suppression_key", f"trg:{trg_id}")
        if supp_key in SUPPRESSION_LOG:
            continue

        try:
            composed = compose(category, merchant, trigger, customer)
        except Exception as e:
            log.exception("compose() failed for trigger %s: %s", trg_id, e)
            continue

        if not composed.get("body"):
            continue  # restraint: nothing worth sending

        SUPPRESSION_LOG[supp_key] = body.now
        conv_id = f"conv_{merchant_id}_{trg_id}"
        CONVERSATIONS.setdefault(conv_id, {"turns": [], "merchant_id": merchant_id,
                                             "customer_id": customer_id})

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
            "template_params": [merchant.get("identity", {}).get("name", "")],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyRequest):
    result = compose_reply(body.conversation_id, body.merchant_id, body.customer_id,
                            body.from_role, body.message, body.turn_number)
    if result.get("action") == "send" and not result.get("body"):
        raise HTTPException(status_code=400, detail="malformed: send action with empty body")
    return result


@app.post("/v1/teardown")
async def teardown():
    CONTEXTS.clear()
    CONVERSATIONS.clear()
    SUPPRESSION_LOG.clear()
    SENT_BODIES.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
