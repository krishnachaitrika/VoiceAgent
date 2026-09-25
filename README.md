# {COMPANY_NAME} Voice Agent — {AGENT_NAME} v3.0 (Enterprise Layer)

A production-ready AI voice agent. When someone calls the configured phone number, the agent answers, understands the caller in English, answers questions from the knowledge base, saves leads, books meetings, and escalates to a human if needed.

**Everything is configured via `.env` — there is no hardcoded agent name, company name, greeting, timezone, or model anywhere in the codebase.** Rename the agent, rebrand the company, or change the calendar timezone by editing `.env` and restarting — zero code edits, anywhere, ever.

This build includes the full v1 → v1.1 → v2 → v3+ roadmap. Every enterprise feature is **feature-flagged** (see `.env.example`) and **fails open** — if Redis is unavailable, the agent falls back to safe in-process behaviour rather than dropping the call.

## Architecture — LangGraph only

**There is exactly one agent brain in this codebase: the LangGraph 4-agent orchestrator (`backend/orchestrator/`).** An earlier version of this project also had a single-call GPT-4o path with a toggle to switch between the two — that path has been removed entirely, not just disabled, so there's exactly one place tool-calling logic lives instead of two copies to keep in sync.

```
Twilio Phone Call → FastAPI WebSocket → ElevenLabs STT → LangGraph Orchestrator → Sarvam/ElevenLabs TTS → Caller
                                                           │
                                    Receptionist (gpt-4o-mini, routes)
                                          │        │         │
                                    Knowledge   Action   Escalation
                                    (GPT-4o)   (GPT-4o)   (GPT-4o)
                                                           │
                                          Redis (session/LLM/RAG cache, event bus, sticky routing)
                                                           │
                                                     Supabase (pgvector + PostgreSQL)
                                                           │
                                                  Next.js Dashboard (localhost:3000)
```

`brain/agent.py` still exists — but only as a thin wrapper that every turn passes through regardless of which specialist agent answers it: input/output guardrails, the Redis LLM cache, and conversation memory. The actual routing and tool-calling logic all lives in `orchestrator/`.

**Speed & accuracy optimizations built into the orchestrator** (so it doesn't cost you the latency of a second full GPT-4o call on top of routing):

1. **Fast router model** — the Receptionist classifies intent using `gpt-4o-mini` (`ORCHESTRATOR_ROUTER_MODEL`), not full GPT-4o — 3-5x faster routing.
2. **Parallel RAG pre-fetch in the Knowledge agent** — pgvector search starts at the same time as the GPT-4o call instead of after it.
3. **Sticky routing** — short follow-up replies mid-flow (a name, a phone number, a "yes") skip the Receptionist call entirely and go straight back to whichever specialist agent was already active, cached in Redis for `STICKY_ROUTE_TTL` seconds (default 120s).
4. **Confidence fallback** — if the router isn't confident (< 0.55) about routing to `action` or `escalation` (the two routes with side effects), it falls back to `knowledge` instead of guessing wrong.

## What's new in this build (v2 + v3+)

| Feature | Where | Flag |
|---|---|---|
| Rolling summary memory | `backend/brain/memory.py` | `ENABLE_ROLLING_MEMORY` |
| Document upload UI (knowledge base) | `backend/api/documents.py`, `frontend/app/documents` | — always on |
| Redis session / LLM / RAG cache + sticky routing | `backend/cache/`, `backend/orchestrator/graph.py` | `REDIS_ENABLED`, `REDIS_*_TTL`, `STICKY_ROUTE_TTL` |
| Redis Streams event bus | `backend/events/bus.py` | `ENABLE_EVENT_BUS` |
| Separate guardrails middleware | `backend/guardrails/` | `ENABLE_GUARDRAILS` |
| Post-call sentiment analytics | `backend/analytics/sentiment.py`, `frontend/app/analytics` | `ENABLE_SENTIMENT_ANALYTICS` |
| LangGraph 4-agent orchestrator (Receptionist/Knowledge/Action/Escalation) | `backend/orchestrator/` | always on — the only brain |
| Fully dynamic branding (name, company, greeting, timezone) | `backend/config.py`, `frontend/lib/AgentConfigContext.jsx` | `AGENT_NAME`, `COMPANY_NAME`, `GREETING_TEMPLATE`, `GOOGLE_CALENDAR_TIMEZONE` |
| Docker Compose (Redis + backend + frontend) | `docker-compose.yml` | — |
| Kubernetes manifests (EKS/AKS) | `k8s/` | — |
| STT migrated to ElevenLabs Scribe v2 / Scribe v2 Realtime (was Sarvam) | `backend/voice/stt.py` | `ELEVENLABS_STT_*` — see "Voice Providers" below |
| TTS provider switch (Sarvam default / ElevenLabs when a voice ID is set) — now covers both batch and live streaming | `backend/voice/tts.py` | `voice_provider` + `elevenlabs_voice_id` (Settings dashboard), or `ELEVENLABS_VOICE_ID` in `.env` |

---

## Local enterprise stack (Redis in Docker)

```bash
cp .env.example .env    # fill in your keys — including AGENT_NAME/COMPANY_NAME if you want to rebrand
docker compose up --build
```

This starts Redis, the FastAPI backend, and the Next.js dashboard together. Backend connects to Redis at `redis://redis:6379/0` automatically inside the compose network. Supabase/Postgres stays external (managed), matching the architecture plan.

If you're already running Redis in Docker yourself outside this compose file, just point `REDIS_URL` in `.env` at it (e.g. `redis://localhost:6379/0` if it's exposed on the host) — no code changes needed.

---

## Renaming the agent / rebranding the company

Every one of these lives in `.env` — none of them are hardcoded in the codebase:

| Variable | Controls |
|---|---|
| `AGENT_NAME` | The agent's name — used in the system prompt, the spoken greeting, `/health`, the dashboard sidebar/title (live, no rebuild) |
| `COMPANY_NAME` | Company name — same places as above, plus the Google Calendar event title |
| `COMPANY_DESCRIPTION` | The one-line company description inside the system prompt |

> **The system prompt itself is not editable at runtime.** It lives in
> `backend/brain/prompts.py` — version controlled, reviewable in a diff, and changed by
> deploy. It was previously a dashboard-editable database row, which meant the
> instructions governing every customer call could change with no diff, no review and no
> history. `POST /api/settings` now rejects the key server-side, so removing the field
> from the UI is not the only thing standing in the way.
| `GREETING_TEMPLATE` | The exact words spoken at the start of every call — `{agent_name}`/`{company_name}` auto-fill from the two vars above |
| `GOOGLE_CALENDAR_TIMEZONE` | Timezone used for booked meetings |
| `APP_VERSION` | Shown in `/health`, `/ready`, and the FastAPI docs |

Change any of these in `.env` and restart the backend — that's it. The dashboard (Sidebar, page titles) also reads `agent_name`/`company_name` live from the Supabase `settings` table via the Settings page, so you can rebrand without even restarting the backend, just by editing it there.

---

## Prerequisites

- **Python 3.11**
- **Node.js 18+**
- **Docker** (for Redis — or point `REDIS_URL` at any Redis instance)
- **Supabase account** (or any Postgres 15+ with the `pgvector` extension available) — Supabase free tier works at [supabase.com](https://supabase.com); only its Postgres connection string (`DATABASE_URL`) is used, not a separate Supabase client
- **ElevenLabs account** — required for STT (Scribe v2/Scribe v2 Realtime has no fallback); free tier works for dev/testing but has no commercial usage rights and limited credits/concurrency — see [elevenlabs.io](https://elevenlabs.io)
- **Sarvam AI account** — used for TTS (default spoken voice) — free credits at [dashboard.sarvam.ai](https://dashboard.sarvam.ai)
- **Twilio account** — $15 free credit at [twilio.com](https://twilio.com)
- **OpenAI API key** — [platform.openai.com](https://platform.openai.com)
- **ngrok** — free at [ngrok.com](https://ngrok.com)

---

## Setup Steps

### Step 1 — Clone and install dependencies

```bash
# Backend
cd backend
pip install -r requirements.txt

# Frontend

cd ../frontend
npm install
```

### Step 2 — Environment variables

```bash
cp .env.example .env
# Open .env and fill in all values
```

Required values:
- `OPENAI_API_KEY` — your OpenAI key
- `ELEVENLABS_API_KEY` — required, STT has no fallback provider
- `SARVAM_API_KEY` — used for TTS (default spoken voice)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` — from Twilio console
- `DATABASE_URL` — Postgres connection string (from your Supabase project's Project Settings → Database, if using Supabase). No separate Supabase URL/API key is needed — the app only ever connects via `DATABASE_URL`.
- `REDIS_PASSWORD` — required when running Redis via `docker-compose.yml` (it refuses to start without one); pick any strong value, `docker-compose.yml` wires it into both the Redis container and the backend's `REDIS_URL` automatically.

### Step 3 — Set up the database

```bash
cd backend
python scripts/setup_db.py
```

Enables pgvector, applies every Alembic migration, creates the HNSW vector index on
`documents.embedding`, and seeds any missing settings.

Safe to re-run: migrations that have already been applied are skipped, and existing
settings are **left alone** rather than overwritten.

If this database predates Alembic it will say so and ask you to run `alembic stamp head`
once — that writes a version marker only, executing no DDL and touching no data.

### Step 3b — Create your dashboard login

The dashboard requires a sign-in. **No default account is seeded** — a shipped username
and password is one nobody remembers to change.

```bash
python scripts/create_user.py
```

You are prompted for a username and password; the password is read without echoing, so it
never reaches your shell history or the process list. Minimum 12 characters, stored as an
Argon2id hash.

Management:

```bash
python scripts/create_user.py --list
python scripts/create_user.py --reset-password <username>
python scripts/create_user.py --unlock <username>       # after 5 failed attempts
python scripts/create_user.py --deactivate <username>
```

Note: deactivating stops the next login but does not kill a live session until it expires
(`DASHBOARD_SESSION_TTL_HOURS`, default 12). To end every session immediately, rotate
`DASHBOARD_SESSION_SECRET` and restart.

### Step 4 — Ingest the knowledge base

```bash
python scripts/ingest_documents.py
```

This chunks `backend/documents/technozis_enquiry.md`, embeds each chunk with OpenAI `text-embedding-3-small`, and stores them in pgvector for semantic search.

### Step 5 — Start the backend

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Verify it's running: [http://localhost:8000/health](http://localhost:8000/health)

### Step 6 — Expose backend with ngrok

Open a **new terminal**:

```bash
ngrok http 8000
```

Copy the `https://xxxx.ngrok.io` URL and:
1. Paste it into your `.env` as `NGROK_URL=https://xxxx.ngrok.io`
2. Go to Twilio Console → Phone Numbers → Your number → Voice webhook → set to `https://xxxx.ngrok.io/incoming-call`

### Step 7 — Start the frontend dashboard

```bash
cd frontend
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

### Step 8 — Test the pipeline (optional)

Without making a real call, test STT → GPT-4o → TTS:

```bash
cd backend
python scripts/test_pipeline.py
```

This records 5 seconds from your microphone, transcribes it, processes through the agent, and plays back the response.

### Step 8b — Verify the QA remediation (optional)

Two scripts check the fixes from the formal test plan. Both exit non-zero on failure, so
they drop straight into CI.

```bash
python scripts/verify_fixes.py     # erasure formats, KB cache versioning, retrieval band
python scripts/verify_round8.py    # escalation no-op guarantee, PII redaction, DB ceiling
```

`verify_fixes.py` writes to the database, but only rows it creates itself, namespaced with
a run marker and removed in a `finally` block even when a check fails.

To send a real test alert once a notification channel is configured:

```bash
python scripts/verify_round8.py --send-test
```

### Step 9 — Make your first call

Dial your Twilio phone number from your mobile. Velu will answer and greet you in English. The agent is English-only — it no longer detects or switches language mid-call.

---

## Project Structure

```
technozis-voice-agent/
├── backend/
│   ├── voice/          # STT (ElevenLabs Scribe v2), TTS (Sarvam Bulbul default / ElevenLabs), VAD, WebSocket stream
│   ├── brain/          # process_turn wrapper (guardrails/cache/memory) + tools — see "Agent Tools" below
│   ├── rag/            # pgvector embedding, ingestion, semantic search
│   ├── database/       # SQLAlchemy models, async CRUD, DB session
│   ├── api/            # FastAPI routers: Twilio webhook, WebSocket, dashboard, settings
│   ├── documents/      # technozis_enquiry.md — knowledge base
│   ├── scripts/        # setup_db.py, ingest_documents.py, test_pipeline.py
│   ├── main.py         # FastAPI app entry point
│   ├── config.py       # All env vars
│   └── requirements.txt
└── frontend/
    ├── app/            # Next.js App Router pages
    ├── components/     # Reusable UI components
    └── lib/api.js      # All Axios calls to FastAPI
```

---

## Agent Tools

Knowledge-base search is **not** an LLM-invoked tool — the Knowledge specialist (`orchestrator/agents.py`) runs the pgvector search directly against the caller's own utterance and puts the result straight into its prompt, before the one GPT-4o call for that turn (see the module's docstring for why: this removes a mandatory second round-trip the old tool-based `search_kb` design cost on every knowledge turn). The tools below are the ones GPT-4o can actually call:

| Tool | When Used | What It Does |
|------|-----------|--------------|
| `save_lead` | Caller shares name/interest | Saves lead to Supabase leads table |
| `confirm_booking_details` | Caller wants to book a meeting | Reads back the proposed date/time/contact details for the caller to confirm before booking |
| `book_meeting` | Caller confirms a meeting time | Creates Google Calendar event + DB record |
| `escalate` | Caller angry / asks for human | Saves escalation + marks call as escalated |

---

## Dashboard Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/dashboard` | Stats overview + recent calls |
| Calls | `/calls` | All calls with transcript viewer |
| Leads | `/leads` | All leads + CSV export |
| Meetings | `/meetings` | Booked meetings from `book_meeting` |
| Escalations | `/escalations` | Resolve unresolved escalations |
| Documents | `/documents` | Upload/manage knowledge-base documents |
| Analytics | `/analytics` | Post-call sentiment + event feed |
| Mock Interview | `/interview` | Spoken practice interview for any role, with a scored report (see below) |
| Settings | `/settings` | Edit agent name, company name, voice provider, ElevenLabs voice ID |

---

## Mock Interview (browser)

A spoken practice interview in the dashboard at `/interview`. Enter a role, experience level and number of questions; the interviewer writes questions for that role, reads each one aloud, listens to the spoken answer, asks at most one follow-up when an answer is thin, and ends with a scored report (overall score out of 5, a hire recommendation, strengths, things to work on, and a score and comment per question).

It is separate from the phone agent: nothing in `voice/`, `brain/` or `orchestrator/` imports it, so it cannot change how live calls behave. It reuses the shared OpenAI client, the input guardrail, Redis and TTS.

| Piece | Where |
|---|---|
| Interview flow (start, answer, follow-up, skip, report) | `backend/mock_interview/engine.py` |
| Every instruction sent to the model | `backend/mock_interview/prompts.py` |
| Session storage (Redis, in-process fallback, expires after `INTERVIEW_SESSION_TTL`) | `backend/mock_interview/store.py` |
| API routes under `/api/interview/...` | `backend/api/interview.py` |
| Page | `frontend/app/interview/page.jsx` |
| Browser speech-to-text hook | `frontend/lib/useSpeechRecognition.js` |
| Tests | `backend/tests/test_mock_interview.py` |

**Voice in:** the browser's own speech recognition (Chrome or Edge). The browser only allows the microphone on `http://localhost` or HTTPS, so open the dashboard at `http://localhost:3000`. In other browsers the candidate types instead.

**Voice out:** the agent's voice from the Settings page (Sarvam by default, your ElevenLabs voice when selected), via `POST /api/interview/speak`. If that fails the page switches to the browser's built-in voice.

**Models:** questions and the report use `INTERVIEW_QUESTION_MODEL` / `INTERVIEW_REPORT_MODEL` (default `gpt-4o`); per-answer evaluation uses `INTERVIEW_TURN_MODEL` (default `gpt-4o-mini`) because the candidate waits on it every turn. All interview settings are in `config.py` under "Mock interview" and can be overridden in `.env`.

Sessions are practice data and are not written to Postgres; copy a report from the report screen to keep it.

---

## Voice Providers

**STT (speech-to-text, what the agent hears):** ElevenLabs Scribe v2 (batch) / Scribe v2 Realtime (live calls) — **required, no fallback provider**. Sarvam is not used for STT at all any more. Language is forced to English (`language_code=en`) at the source rather than auto-detected — see `voice/stt.py`. Other tunables via `ELEVENLABS_STT_*` in `.env` (model, audio format, VAD timing — see comments in `config.py` for what each one does and when to touch it).

**TTS (text-to-speech, the agent's spoken voice):**
- **Default:** Sarvam Bulbul v3 — spoken in English
- **Optional:** ElevenLabs with your cloned voice — set both `voice_provider=elevenlabs` and a real `elevenlabs_voice_id` (Settings dashboard, or `ELEVENLABS_VOICE_ID` in `.env`). Applies to both single-shot and live-call streaming TTS. If ElevenLabs fails to connect mid-call, this falls back to Sarvam automatically rather than leaving the caller with dead air.

### Filler-word filtering — still needed, unrelated to the STT provider

`is_filler_transcript()` in `voice/stream.py` drops one/two-word throwaway transcripts ("okay", "yeah", "hmm", "thank you") so they don't trigger a full agent turn on nothing. This is **content-level filtering on the words themselves**, not an STT-specific hallucination workaround — it's needed regardless of which STT provider is active, because real callers say "okay" and "yeah" as standalone utterances no matter how accurate the transcription is. No change needed here for the ElevenLabs migration.

Separately, `voice/stt.py` strips ElevenLabs' bracketed non-speech tags (`[static]`, `[noise]`, `[silence]`, etc.) before a transcript is treated as caller speech — this **is** ElevenLabs-specific (Sarvam didn't emit tags like this), and is a different mechanism from the filler filter above: one strips noise labels, the other drops meaningless-but-real words.

### Noise cancellation — not a separate ElevenLabs feature we control

There's no dedicated "noise cancellation" toggle on ElevenLabs' STT API — Scribe's robustness to background noise/accents is a property of the model itself, not a parameter this codebase can turn on/off. What we *do* control:
- The bracketed non-speech tagging above (Scribe labels noise instead of guessing words from it)
- `ELEVENLABS_STT_VAD_THRESHOLD` / `ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS` in `.env` — tunes how sensitive server-side speech detection is, which indirectly affects how noise-prone segments get handled
- Twilio's own line-level audio is what actually reaches ElevenLabs — no additional denoising happens in this codebase before that

If real call data shows noise is still a problem after tuning the VAD settings above, that's a Twilio-side or physical-audio-quality issue, not something fixable via an ElevenLabs API parameter.

---

## Google Calendar Setup (optional)

1. Create a Google Cloud project and enable the Calendar API
2. Create a Service Account and download `credentials.json`
3. Place `credentials.json` in the `backend/` folder
4. Set `GOOGLE_CALENDAR_ID=primary` (or your calendar ID) in `.env`
5. Share your calendar with the service account email

If Google Calendar is not configured, meeting requests are still saved to the database.

---

## Cost Tracking

Every call tracks:
- **OpenAI cost** — GPT-4o token usage per turn, summed per call
- **Speech cost** (TTS + STT, whichever provider handled each) — metered from characters synthesized (TTS) and call duration (STT); **requires `ELEVENLABS_TTS_COST_PER_1K_CHARS`, `SARVAM_TTS_COST_PER_1K_CHARS`, and `ELEVENLABS_STT_COST_PER_MINUTE` to be set in `.env`** to your actual negotiated rate — they default to `0.0`, since this codebase can't know your rate on its own (see `config.py`'s "Speech provider cost metering" section)
- **Total cost** — displayed in dashboard per call (OpenAI + speech, once the rates above are configured)

---

## QA & Audits

- [`QA_DEFECT_TRACKER.md`](QA_DEFECT_TRACKER.md) — live register of the 22 pre-test findings and their remediation status.
- [`docs/`](docs/) — the underlying audit documents (pre-test findings and remediation audit, `.docx`/`.pdf`).

---

## Troubleshooting

**Twilio can't reach my server:** Make sure ngrok is running and the webhook URL in Twilio console matches your ngrok URL exactly (with `/incoming-call`).

**STT returns empty / caller not heard:** Check your `ELEVENLABS_API_KEY` — STT has no fallback provider, every call fails to transcribe without it. Test with `python scripts/test_pipeline.py`.

**Transcripts in the wrong language:** shouldn't happen — STT is forced to English (`language_code=en`) at the source in `voice/stt.py`, there's no auto-detection to misfire any more. If you're still seeing garbled/non-English transcripts, it's an audio-quality issue (check the mulaw/PCM conversion), not a language-detection one.

**Phone numbers / long digit strings getting split or a digit dropped:** Callers naturally pause between digit groups when reading a number aloud, and VAD-based auto-commit can finalize the segment on one of those pauses. Try raising `ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS` in `.env` (default `1.0`) to give more room between digit groups before the segment commits.

**`websockets library is older than v13` warning:** Run `pip install -r requirements.txt` again in your venv to get the pinned `websockets==13.1`. The code has a compatibility fallback so this is a warning, not a hard failure — but the pinned version is what's actually been tested.

**pgvector error on setup:** Make sure your Supabase plan supports pgvector (all plans do as of 2024). Run `python scripts/setup_db.py` again.

**Frontend shows "Failed to connect":** Make sure the backend is running on port 8000. Check the `next.config.js` proxy setting.