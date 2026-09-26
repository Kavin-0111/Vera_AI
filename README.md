# Vera-replacement bot — magicpin AI Challenge

## Approach

A single FastAPI service implementing the 5-endpoint contract
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`).

The composer (`compose()` in `bot.py`) is one function that takes the four
context layers (category, merchant, trigger, customer?) and produces a
message via Claude at `temperature=0`, guided by a system prompt that
encodes the brief's rubric directly: specificity, category-voice fit,
merchant personalization, trigger relevance, and engagement-compulsion
levers (with an explicit nudge toward the underused "social proof" and
"ask the merchant a question" levers called out in the brief).

If no `ANTHROPIC_API_KEY` is set, the bot falls back to deterministic
rule-based templates per trigger kind, so the service still runs
end-to-end (lower quality, but never crashes or times out).

## Multi-turn handling (`/v1/reply`)

- **Auto-reply detection**: matches canned-reply phrase patterns (English +
  Hindi) and also treats an exact-repeat of a prior message from the same
  role as a signal. First hit -> one low-friction binary retry. Second hit
  -> graceful `end` (per the brief's "auto-reply pollution" pain point:
  don't burn multiple turns on a bot).
- **Intent detection**: explicit positive ("yes"/"go ahead"/"chalega")
  routes straight to `action mode` instead of re-qualifying (avoids the
  brief's Pattern D anti-pattern). Explicit negative ends the conversation
  immediately.
- **Neutral turns**: routed back through the LLM with the running
  conversation + contexts, or a safe fallback if no LLM is configured.
  Conversations auto-exit after 6 turns with no clear signal.

## Anti-repetition

Every body sent to a given merchant is tracked in-memory
(`SENT_BODIES`); an exact repeat is nudged before sending, per the
testing brief's `-2 per repeat` penalty.

## Suppression

`suppression_key` from each trigger is logged on first use and won't
fire again in `/v1/tick` for the same key — avoids re-notifying on the
same event.

## Tradeoffs

- In-memory state only (no Redis/DB) — fine for a single 60-minute test
  window per the brief, would need persistence for production.
- The rule-based fallback composer covers only the trigger kinds seen in
  the sample dataset; an LLM key is required to handle arbitrary/novel
  trigger kinds well.
- No embedding-based retrieval over `digest`/`patient_content_library` —
  the full category context is passed to the LLM directly (context sizes
  in the dataset are small enough that this is simpler and just as
  accurate for this scale).

## What additional context would have helped

- Ground-truth "good message" examples per trigger kind (beyond the two
  in the brief's appendix) would have let us tune the rule-based fallback
  more precisely.
- A recommended maximum reply latency budget per turn (we assume the
  stated 30s hard limit is also the target, not just the cutoff).

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...        # optional; omit to use rule-based fallback
export TEAM_NAME="Your Team"
export TEAM_MEMBERS="Alice,Bob"
export CONTACT_EMAIL="you@example.com"
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then, from the challenge zip's root, run the provided simulator against it:

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

## Deploying

`Dockerfile` included — deploy to Render / Railway / Fly.io / Cloud Run,
set the same env vars, and submit the resulting public HTTPS URL.
