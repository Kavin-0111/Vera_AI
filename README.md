# Vera-replacement bot — magicpin AI Challenge

## Project Details

A single FastAPI service implementing the 5-endpoint contract (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`).

The composer (`compose()` in `bot.py`) is one function that takes the four context layers (category, merchant, trigger, customer?) and produces a message via a large language model at `temperature=0`. It is guided by a system prompt that encodes the brief's rubric directly: specificity, category-voice fit, merchant personalization, trigger relevance, and engagement-compulsion levers.

**Recent PRD Upgrades Implemented:**
- **FR-12 & NFR-3 (Timeout & Async):** `compose()` runs asynchronously in a ThreadPoolExecutor with a hard 25-second timeout, ensuring the bot never exceeds the 30s judge limit.
- **FR-18 (Hostile/Off-topic Handling):** Fully implemented hostile language and off-topic redirect loops.
- **NFR-5 (Action Limits):** Hard cap of 20 actions per `/v1/tick` implemented.
- **FR-1 (Idempotency):** Strict versioning handling on `/v1/context` (returns 200 for same version, 409 for stale version).
- **C-1 (WhatsApp Sessions):** Enforces template matching only on the first outbound message.

## Team Info
- **Team Name:** Kavin
- **Team Members:** Kavin Mathur
- **Contact Email:** kavin.mathur.ug23@nsut.ac.in

## Deployment

The bot is actively deployed using **Docker** on **Render's Free Tier**, powered by the **Google Gemini 2.0 Flash** API.

- **Base Deployment URL:** `https://vera-ai-bot-obtb.onrender.com`
- **LLM Provider:** Google Gemini (`gemini-2.0-flash`)
- **Hosting:** Render (Docker Runtime)

> **⚠️ Note on Render Free Tier:** Render spins down inactive free instances after 15 minutes. To avoid disqualification from a cold-start timeout during judging, a ping service (like UptimeRobot) should be configured to hit the `/v1/healthz` endpoint every 5 minutes during the active testing window.

## Endpoints to Test

You can verify the active deployment using the following endpoints. 

**GET Endpoints (Clickable in browser):**
1. **Health Check:** [https://vera-ai-bot-obtb.onrender.com/v1/healthz](https://vera-ai-bot-obtb.onrender.com/v1/healthz)
   - *Expected:* `{"status": "ok", "uptime_seconds": ... }`
2. **Metadata:** [https://vera-ai-bot-obtb.onrender.com/v1/metadata](https://vera-ai-bot-obtb.onrender.com/v1/metadata)
   - *Expected:* JSON containing the Team Name, Members, and Bot Version (`0.2.0`).

**POST Endpoints (Requires curl or Postman):**

3. **Push Context (`/v1/context`)**
```bash
curl -X POST https://vera-ai-bot-obtb.onrender.com/v1/context \
-H "Content-Type: application/json" \
-d '{
  "scope": "merchant",
  "context_id": "merch_123",
  "version": 1,
  "payload": {"identity": {"name": "Test Salon"}},
  "delivered_at": "2026-09-26T10:00:00Z"
}'
```

4. **Tick (`/v1/tick`)**
```bash
curl -X POST https://vera-ai-bot-obtb.onrender.com/v1/tick \
-H "Content-Type: application/json" \
-d '{
  "now": "2026-09-26T10:00:00Z",
  "available_triggers": []
}'
```

## Running Locally

No LLM provider name is hardcoded anywhere in `bot.py` — all provider specifics are supplied via environment variables.

1. Ensure you have your `.env` file configured with your Gemini API key (see `.env` for the template, this file is intentionally git-ignored).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the FastAPI server:
   ```bash
   uvicorn bot:app --host 0.0.0.0 --port 8080
   ```
4. Run the provided simulator from the challenge root:
   ```bash
   export BOT_URL=http://localhost:8080
   python judge_simulator.py
   ```
