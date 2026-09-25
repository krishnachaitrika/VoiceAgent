# Voice Agent — QA defect tracker

Tracks the 22 findings from [`docs/Velu_PreTest_Findings.docx`](docs/Velu_PreTest_Findings.docx)
(static review, 10 Sep 2026) through two rounds of fixes. Status verified by diffing three
repository snapshots, reading every new module, running the test suite, and starting the
application.

Full narrative version: [`docs/Voice_Agent_Remediation_Audit.docx`](docs/Voice_Agent_Remediation_Audit.docx) / [`.pdf`](docs/Voice_Agent_Remediation_Audit.pdf)
(now includes a section 9 addendum for the round-three sweep below; a section 10 for
round four is still to be written).

**Last updated:** 19 Sep 2026 · **Snapshot:** post round-eight, all 15 test-plan findings addressed

## Summary

**Original 22-finding register** (unchanged by round 3 — see the separate sweep below):

| | Baseline | Round 1 | Round 2 |
|---|---|---|---|
| Fully closed | 0 | 6 | **17** |
| Partial | 0 | 2 | 4 |
| Untouched | 22 | 14 | 1 |

Verified directly: `/api/dashboard/stats` → 401 unauthenticated · `/ready` → 503 with
`database_connected: false` · rate limiter first 429 at request 60 · test suite **50 passed**, CI green.

**Round 3 — independent sweep, 16 Sep 2026** (separate register, see below): 18 new
findings, 16 fixed same-session, 2 referred to the client for an architecture decision
rather than patched unilaterally.

## Register

| ID | Severity | Status | Where it was fixed |
|---|---|---|---|
| VA-A1 no test infrastructure | HIGH | ✅ R1 | `requirements-dev.txt`, `pytest.ini`, `tests/`, `.github/workflows/tests.yml`. Thin — `fakeredis`/`testcontainers` pinned but unused, no HTTP-client or DB fixture tests |
| VA-A2 import-time dependencies | HIGH | ✅ R1 | `services/providers.py::get_openai_client()`, `database/base.py::get_engine()`, both `@lru_cache` |
| VA-A3 960-line call pipeline | HIGH | ⚠️ partial R1 | `voice/turn_state.py` (`TurnHoldState`, injectable clock, 12 tests). `handle_twilio_stream` still ~910 lines; barge-in, turn preemption, playback still closures |
| VA-A4 no migrations | MEDIUM | ✅ R1 | Alembic + hand-written `0001_baseline_schema`. No CI check against `models.py`; `setup_db.py` still does `create_all` |
| VA-A5 unstructured logs | MEDIUM | ✅ R1 | `logging_utils.py` — contextvar `call_id`/`turn_id` + `logging.Filter` + JSON formatter. uvicorn's own loggers not reconfigured (mixed format) |
| VA-B1 unauthenticated dashboard API | CRITICAL | ⚠️ **partial** | `auth.py`, two tiers, `hmac.compare_digest`, fails closed. **Frontend proxy still attaches the admin key to every request and the dashboard has no login** (unchanged by R3 — the client chose to scope the public ingress down instead, see R3-E1/9.5 below, and to leave the login gap documented rather than build it this round) |
| VA-B2 Twilio signature unvalidated | CRITICAL | ✅ R1 | `twilio_auth.py` — `RequestValidator` on the webhook + HMAC stream token (30s TTL). Token replayable inside its TTL |
| VA-B3 no rate limit / concurrency cap | HIGH | ✅ R2 | slowapi + `MAX_CONCURRENT_CALLS` semaphore in `api/websocket.py`; DB pool resized to match |
| VA-B4 KB wipeable by anon request | HIGH | ✅ R2 | Admin tier (R1) + `?confirm=true` and source validation (R2) |
| VA-B5 PII unmasked, no retention | HIGH | ⚠️ partial R2 | `utils/privacy.py`, `crud.purge_calls_older_than` / `delete_caller_data`, `DELETE /callers/{phone}`, `scripts/purge_old_data.py`. Nothing schedules the purge; DB still plaintext; exact-string phone match |
| VA-B6 prompt injection via KB docs | HIGH | ✅ R2 | Fenced BEGIN/END markers + explicit treat-as-data rule in `orchestrator/agents.py` |
| VA-C1 no LLM timeout/retry | CRITICAL | ✅ R2 | `timeout=8.0, max_retries=1` on the shared client; `brain/agent.py` now records the escalation it promises. **Bounds the request, not the turn** — see R4 |
| VA-C2 cost tracking wrong | HIGH | ⚠️ partial R2 | `StreamingTTSSession.cost_usd()` + duration-based STT metering. All three rates default to `0.0`; batch TTS path unmetered; field still named `sarvam_cost` |
| VA-C3 dashboard can't reach backend | HIGH | ✅ R1+R2 | `app/api/[...path]/route.js` + `BACKEND_INTERNAL_URL`; `NEXT_PUBLIC_API_URL` removed everywhere in R2 |
| VA-C4 readiness ignores DB | HIGH | ✅ R2 | `SELECT 1` in `/ready`, 503 on failure; Redis stays non-fatal |
| VA-C5 no webhook idempotency | MEDIUM | ✅ R2 | `call_id = call_sid`; `create_call` / `save_transcript` idempotent |
| VA-C6 no graceful shutdown | MEDIUM | ✅ R2 | `begin_shutdown` / `wait_for_drain` + `terminationGracePeriodSeconds: 45` |
| VA-C7 unvalidated meeting datetimes | MEDIUM | ✅ R2 | `_validate_meeting_datetime` — past / >60 days / business hours; no longer books "now" on a parse failure |
| VA-D1 dead code | LOW | ✅ R2 | `signalwire_webhook.py`, `agents/`, `search_kb` all removed |
| VA-D2 docs contradict code | LOW | ✅ R2 | ConfigMap greeting no longer promises Tamil/Hindi; README and `guide` updated |
| VA-D3 vestigial language parameter | LOW | ❌ **open** | `lang` still threaded stream → agent → orchestrator as a constant |
| VA-D4 dev deps in production | LOW | ✅ R2 | `sounddevice` / `numpy` moved to `requirements-dev.txt` |

## Round 3 — independent full-codebase sweep (16 Sep 2026)

A separate audit pass, run independently of the 22-finding register above (not
cross-referenced against it going in), covering the whole repository for security,
performance and completeness gaps. Full narrative treatment in section 9 of the audit
docx/pdf linked above. 16 of 18 findings fixed and verified same-session (50 tests still
passing, dashboard production build clean, new Alembic revision resolves to one head);
2 were architecture-level calls referred to the client rather than patched — see notes.

| ID | Severity | Status | Where it was fixed |
|---|---|---|---|
| R3-E1 k8s ingress published the whole backend, not just Twilio's 2 entry points | HIGH | ✅ | `k8s/05-frontend-deployment.yaml` — narrowed to exact-match `/incoming-call` + `/stream`. *Referred to client: remove entirely vs. narrow — narrowing chosen so Twilio still connects* |
| R3-E2 dashboard proxy had no CSRF/path-traversal defense | MEDIUM | ✅ | `frontend/app/api/[...path]/route.js` — Origin/Referer check on non-GET, `..`/`.` segment rejection |
| R3-E3 containers ran as root, no k8s securityContext | MEDIUM | ✅ | both Dockerfiles + `k8s/03-04-05-*.yaml` — non-root user, `runAsNonRoot`, capability drop |
| R3-E4 Redis unauthenticated + host-exposed | MEDIUM | ✅ | `docker-compose.yml` (requirepass, host port removed) + `k8s/03-redis.yaml`/`01-secret.example.yaml` (same, via secret) |
| R3-E5 `CORS_ALLOWED_ORIGINS` missing from k8s configmap | MEDIUM/HIGH | ✅ | `k8s/02-configmap.yaml` — would've silently broken prod dashboard's credentialed requests |
| R3-E6 `python-multipart` CVE-2024-53981 | LOW | ✅ | `requirements.txt` bumped |
| R3-F1 guardrail output-leak patterns too narrow (2 phrasings) | MEDIUM | ✅ | `guardrails/patterns.py` — broadened to 17, wired into `output_guardrail.py` |
| R3-F2 `vad.py` O(n²) buffer rescan every audio frame | HIGH | ✅ | `voice/vad.py` — bounded to a fixed trailing window; see 9.4/F2 for the one accepted edge-case trade-off |
| R3-F3 blocking PDF extraction on the event loop | MEDIUM | ✅ | `api/documents.py` — moved to a worker thread |
| R3-F4 2 full-table/embedding-loading KB queries | MEDIUM | ✅ | `database/crud.py` — moved grouping/filtering into SQL |
| R3-F5 inline `__import__` in `brain/tools.py` | LOW | ✅ | converted to ordinary imports (no real circular import found) |
| R3-G1 Alembic baseline drifted from `models.py` (2 dead columns) | HIGH | ✅ | new revision `backend/alembic/versions/0002_drop_language_columns.py`, chain verified `0001→0002` |
| R3-G2 dead `SUPABASE_URL`/`SUPABASE_KEY` config | MEDIUM | ✅ | removed from `config.py`, `.env.example`, README |
| R3-G3 23 undocumented env vars | LOW | ✅ | added to `.env.example` with one-line descriptions |
| R3-H1 no ESLint config, `next lint` a no-op | MEDIUM | ✅ | added `frontend/.eslintrc.json`; fixed 2 lint errors it surfaced |
| R3-H2 Settings changes didn't propagate live | MEDIUM | ✅ | `AgentConfigContext.jsx` refresh() wired into `settings/page.jsx` |
| R3-H3 out-of-order response races (4 views) | MEDIUM | ✅ | `calls`/`leads`/`meetings` pages + `TranscriptModal.jsx` — cancellation-flag guard |
| R3-H4 analytics "live" feed never polled | LOW | ✅ | `analytics/page.jsx` — matched dashboard's polling pattern |
| R3-H5 a11y label gap + silent escalation-resolve error | LOW | ✅ | `SettingRow.jsx` (id/htmlFor) + `escalations/page.jsx` (setError) |
| R3-H6 6 unused frontend dependencies | LOW | ✅ | removed from `package.json`, lockfile regenerated |

**Referred to the client rather than patched:** the ingress scope (R3-E1 — resolved by
narrowing, not removal) and the dashboard login (VA-B1 — client chose to leave open and
documented, not build one this round). Both are discussed in section 9.5 of the audit
document.

## Round 4 — first live-call validation (16 Sep 2026)

The first end-to-end test against a real Twilio number. Rounds 1-3 were static review,
unit tests and a clean application start; nothing had actually dialled in. Round 3's own
"next batch" item 3 called this out — *"One live call to confirm the Twilio signature
path — the failure mode is total and silent"* — and that is exactly how it failed.

Five defects, all of which required a real call to surface. Four were total-failure bugs
invisible to the 50-test suite, static review and a successful boot. Fixed and confirmed
by a subsequent clean call: 123s, meeting booked, `positive (booked)`, transcript stored,
dashboard 200s throughout.

| ID | Severity | Status | Where it was fixed |
|---|---|---|---|
| R4-I1 stream token never reached the WebSocket — **every inbound call rejected** | CRITICAL | ✅ | `api/twilio_webhook.py` + `api/websocket.py` — credential moved from `?sid=&exp=&token=` into the URL path `/stream/{sid}/{exp}/{token}`. The HMAC in `twilio_auth.py` was correct throughout; the query string was losing the values in transit, so `query.get()` returned `""` and `verify_stream_token` failed closed exactly as designed. Because `<Connect><Stream>` is a terminal TwiML verb, the 1008 close ended the call outright — caller heard one ring, then nothing. A legacy `/stream` route is retained temporarily: it logs `raw_query`/`param_keys`/lengths so a recurrence is diagnosable rather than silent |
| R4-I2 `zoneinfo` has no tz database on Windows — **all bookings failed** | HIGH | ✅ | `requirements.txt` — added `tzdata`. `ZoneInfo("Asia/Kolkata")` raised `No time zone found with key Asia/Kolkata`; Linux/macOS/Docker ship an IANA database, Windows does not. Invisible in CI and in containers, fatal on every developer machine. Surfaced in `_tool_book_meeting` via R3's own `_validate_meeting_datetime` |
| R4-I3 rolling summary destroyed the phone-fidelity digit stream | HIGH | ✅ | `brain/memory.py` — new append-only digit ledger (`get_digit_stream` / `_append_digits`), written once per user turn, never summarized or truncated, same Redis TTL and fallback as the session. `brain/tools.py` reads it in preference to the history rebuild (longer stream wins, so neither path can lose digits the other has). **Two correct features cancelling out:** `_validate_phone_or_reject` rebuilds the caller's ground-truth digits by filtering history for `role == "user"`, but `_apply_rolling_summary` replaces old turns with one `role == "system"` message — so a number given early vanished. Live log: caller said `6369996595` at turn 4, by turn 8 the reconstructed stream was `'100'` (the digits of "1:00 p.m.") and a valid number was rejected. Reading digits out of the summary is *not* an acceptable fix — it is LLM-generated, and the check exists to compare against text the model cannot influence |
| R4-I4 `confirm_booking_details` never received `call_id` | MEDIUM | ✅ | `orchestrator/agents.py` — added to the `tool_args["call_id"]` tuple, kept out of the `state["tool_called"]` tuple (it is a pure read-back and writes nothing). With `call_id=None`, `_validate_phone_or_reject` fell back to an empty digit stream and **silently skipped the fidelity check entirely** — so the read-back, the one step where a human can catch a wrong number, ran only the length check and was the weakest validation in the flow. Visible in the log as `Rejected invalid phone for call_id=None` |
| R4-I5 Google Calendar client blocked the event loop | HIGH | ✅ | `brain/tools.py` — `asyncio.to_thread` + `asyncio.wait_for(GOOGLE_CALENDAR_TIMEOUT_SEC)`; credentials parsed once at module scope instead of re-reading the service-account JSON and re-parsing the RSA key per booking; `cache_discovery=False` (needed for the read-only root filesystem from R3-E3). `google-api-python-client` is synchronous (httplib2), so `build()`/`.execute()` inside an async function froze **every concurrent call** on that worker: no audio to Twilio, no STT chunks, no barge-in, no WebSocket reads. Measured at ~5s in the live log (`13:19:10` → `13:19:15`, 7.8s total turn). Confirmed fixed in the follow-up call — an STT activity line was logged *during* the booking, which the frozen loop could not previously do |

## Rounds 5-8 — the formal test plan (17-19 Sep 2026)

Source: [`docs/Voice_Agent_Test_Plan.xlsx`](docs/Voice_Agent_Test_Plan.xlsx) — 138 planned
cases across 13 categories, 54 executed, plus an AI evaluation suite run against the LIVE
knowledge base rather than a synthetic corpus. That last detail matters: the retrieval
distances it recorded are calibrated to this deployment's real documents, chunk sizes and
embedding distribution, so the thresholds derived from them transfer.

Executed: **40 pass, 12 fail, 2 blocked.** Ten of the twelve failures were `KNOWN DEFECT`
rows written to confirm a defect exists, so only two were new findings.

**14 of 15 closed. VA-T-005 is reported rather than patched** — the fix depends on whether
clients share a database, which is an architecture decision, not an engineering one.

| ID | Severity | Status | Fix |
|---|---|---|---|
| VA-T-001 ingress `pathType: Exact` cannot match the media-stream URL | CRITICAL | ✅ | `k8s/05-frontend-deployment.yaml` → `Prefix`. Introduced by round 4's own fix (the stream credential moved into the path); every call would 404 at the ingress. Invisible locally, where nothing sits in front of uvicorn |
| VA-T-002 dashboard had no login | CRITICAL | ✅ | Per-user accounts, not a shared password: `users` table (migration `0005`), Argon2id hashing, lockout after 5 failures, one generic message for every credential failure so accounts cannot be enumerated, and an unknown username still hashes so it takes the same time as a wrong password. No account is seeded — `scripts/create_user.py` prompts with `getpass`, so the first password is never in shell history or the process list |
| VA-T-003 LLM cache ignored the knowledge base | HIGH | ✅ | Cache key now includes a knowledge-base version (`cache/kb_version.py`) |
| VA-T-004 drain used a `set` keyed on CallSid | HIGH | ✅ | `Counter` — a webhook retry gives one CallSid two connections, and the first to end removed the id while the second was still streaming |
| VA-T-005 400 DB connections at full HPA scale | HIGH | ⚠️ reported | `config_validation.py` computes `MAX_CONCURRENT_CALLS × 2 × MAX_REPLICAS` against `DB_CONNECTION_CEILING` and warns at startup with the options. Not patched: the answer differs for shared-database vs database-per-client, and guessing would bake in the wrong one |
| VA-T-006 rate limiter keyed on the proxy | HIGH | ✅ | `X-Forwarded-For` honoured only from addresses in `RATE_LIMIT_TRUSTED_PROXIES`, walking the chain right-to-left. Trusting the header unconditionally would have been worse than the bug |
| VA-T-007 cost rates defaulted to 0.00 | HIGH | ✅ | Rates set and verified; startup enumerates any left unset. The 0.0 defaults are deliberate — a credible-looking wrong rate is worse than an obvious zero, as an outbound estimate of $0.12 against a real $0.0699 demonstrated |
| VA-T-008 retention purge had no schedule | MEDIUM | ✅ | `k8s/07-retention-cronjob.yaml` — daily, `concurrencyPolicy: Forbid`, more failure history than success history because a failed purge is the one you need logs for |
| VA-T-009 erasure missed other phone formats | MEDIUM | ✅ | Matches on digits via `regexp_replace`, comparing the last `PHONE_NUMBER_DIGIT_LENGTH`. The old exact `==` deleted the lead, left every call and transcript, and reported success |
| VA-T-010 superseded turns still billed | LOW | ✅ | Kept in the total — OpenAI billed it, and under-reporting is the wrong direction for a finance figure — but tracked and logged separately so barge-in tuning is measurable |
| VA-T-011 two 30s drains vs a 45s deadline | HIGH | ✅ | Drains run concurrently under `SHUTDOWN_TOTAL_BUDGET_SEC`; grace period raised to 60s; startup compares the two numbers |
| VA-T-012 hallucination 13.3% | HIGH | ✅ | See the band below |
| VA-T-013 retrieval failure 80% | HIGH | ✅ | See the band below |
| VA-T-014 injection answered "Yes, go ahead!" and was cached | LOW→MED | ✅ | Flagged turns are never cached — a `direct` reply was cacheable, so one caller's probe became every caller's answer for an hour, served straight from cache. Flagged `direct` turns now get a neutral deflection |
| VA-T-015 greeting offered Tamil and Hindi on an English-only build | MEDIUM | ✅ | Corrected, and startup now REFUSES to boot in production if the greeting names an unsupported language |

### VA-T-012 / VA-T-013 — one threshold, both failures

A single pass/fail cut at distance 0.70 produced both measured problems. Every recorded
retrieval failure was a legitimate paraphrase landing just over the line (0.7461, 0.7562,
0.7861), and with nothing retrieved the specialist answered from model training anyway —
once with a factually CORRECT answer and zero retrieval, which is the dangerous case
because nothing flags it until the model and the client's real data disagree.

Replaced with a band: `≤0.55` strong, `0.55-0.80` weak (passage used under explicit
answer-only-from-this rules), `>0.80` refuse. Plus `kb_source=strong|weak|none` logged on
every knowledge turn, so "answered with no retrieval" is now countable rather than
something a QA round has to discover.

**Live call, 19 Sep, one conversation exercising all three bands:**

| Turn | Grounding | Outcome |
|---|---|---|
| 1 | `strong` | answered from the knowledge base |
| 2 | `weak` | **recovered** — would have been refused at 0.70 |
| 3 | `weak` | **recovered** |
| 4 | `none` | *"I don't have that specific information — would you like me to have the team follow up?"* |
| 5 | `none` | refused honestly |
| 6 | — | escalation recorded, `negative (escalated)`; declined to escalate twice on the repeat |

Turns 4 and 5 are the ones that matter: nothing retrieved, and the agent refused instead of
inventing. That is VA-T-013 demonstrated, not inferred.

**The thresholds still want her eval re-run.** 0.55/0.80 are read from the recorded
distances; only Dataset B re-run on the same corpus shows whether hallucination actually
falls from 13.3%.

### Round 8 — the items that were never in her plan

Both are **no-ops until configured**, verified rather than assumed: with nothing set,
`dispatch_escalation` returns in ~0.005ms and creates zero tasks, and the Sentry SDK is
never imported.

- **Escalation notified nobody.** `escalate` wrote a row, flipped the status, published an
  event — and told no human, while telling the caller their team would follow up. Slack,
  SMS and generic webhook, fired concurrently off the call path. Caller number masked,
  transcript excerpt truncated: Slack usually has a wider audience and longer retention
  than the database.
- **No error tracking.** Optional Sentry with two-layer PII redaction —
  `send_default_pii=False` stops the SDK volunteering data, and a `before_send` hook strips
  phone numbers from message text, which is what catches numbers our OWN log lines put into
  an exception string.
- **System prompt removed from runtime.** It lived in a dashboard-editable database row, so
  the instructions governing every customer call could change with no diff, no review and
  no history. Now code only (`brain/prompts.py`), rejected server-side by an allow-list —
  hiding the field would not have been enough, since a request with the admin key could
  still set it.

### Verification

Two scripts, both passing, both runnable in CI:

- `scripts/verify_fixes.py` — **9/9**. Erasure across four phone formats (and two similar
  numbers correctly untouched), knowledge-base version moving on insert AND delete
  (`39 → 40 → 39`), the band classifying ten recorded distances, Argon2id confirmed in the
  database, vector index present
- `scripts/verify_round8.py` — **9/9**. The no-op guarantee, PII redaction, payload masking
  and truncation, the VA-T-005 arithmetic

Not covered by either — these need a proxy, a cluster, or a call: VA-T-001, VA-T-004,
VA-T-006, VA-T-008, VA-T-011.

## Defects introduced by the fixes

### Resolved
- **R0 — CI red on first run.** `pytest` collected `scripts/test_pipeline.py` (PortAudio `OSError`). Fixed in round two by `testpaths = tests`.

### Open

*(Items 1, 3, 5, 6 and the trailing purge note were closed in rounds 5-8 — see the
table above. Left in place for the history rather than deleted.)*

1. ~~**Rate limiter keyed on the wrong IP.**~~ **CLOSED (VA-T-006).** `get_remote_address` with no `X-Forwarded-For` handling. Every dashboard request arrives from the frontend pod, so all users share one 60/min bucket; `/incoming-call`'s 30/min sees the ingress IP, making it a global cap on inbound calls rather than per-client.
2. **`/health` and `/ready` are rate-limited.** Default limit applies to both. Safe at current probe rates, but anything else polling from that source IP can 429 the *liveness* probe into a restart loop. Mark them `@limiter.exempt`.
3. ~~**`_active_calls` drain bug — caused by the C5 fix.**~~ **CLOSED (VA-T-004)** — now a `Counter`. It's a `set` keyed on CallSid, and C5's premise is that a Twilio retry can produce two live sockets for one CallSid. When the first ends, `discard()` removes the id while the second still streams; `wait_for_drain` sees empty and lets the process exit mid-call. Use a counter. Related: nothing rejects the duplicate *connection* — both greet the caller, and the surviving transcript is whichever finishes last.
4. **C1 bounds the request, not the turn.** A turn makes several sequential LLM calls (router → specialist → tool follow-up → retry). Worst case ~16s each stacks past a minute before the caller hears `LLM_FALLBACK_MESSAGE`, while `TURN_PREEMPT_GRACE_MS` is 3000ms. No turn-level budget.
5. **Connection maths at scale.** *(VA-T-005 — reported at startup, awaiting the tenancy decision.)* `pool_size=20 + max_overflow=20` = 40 per pod × HPA `maxReplicas: 10` = 400 Postgres connections, past Supabase's pooler ceiling. The per-pod cap is right; nothing accounts for replicas.

6. ~~**LLM cache serves a degraded cross-caller answer.**~~ **CLOSED (VA-T-003)** — the cache key now includes a knowledge-base version. `cache/llm_cache.py` is keyed on `(user_text, language)` with no call or tenant scope, TTL 1 hour. In the round-four log, turn 1 hit the cache and answered *"Technozis is a company that provides technology services. We have operations in Bangalore, India, and a UK office."* — a weaker, partly inaccurate answer cached from an earlier call. The live KB answer for the same question is *"UK-based enterprise AI and digital transformation services company founded in 2013…"* (top match distance 0.42). So a poor answer generated once is replayed to every subsequent caller for an hour, and RAG never runs to correct it. Consider caching only when the KB search returned a hit above a confidence bar, and keying on a normalized question rather than the raw transcript.
7. **Fragment splitting burns a whole turn.** `MULTI_PART_HOLD_GRACE_MS` is 900ms. In the 13:17:45 call the caller said *"…did you have any, yes, enterprise-level"* then 3s later *"projects in ServiceNow field?"* — one question, split. The grace expired first, so turn 5 ran a full GPT-4o call that was then cancelled, and turn 6 answered the second half. ~$0.026 spent on a discarded turn. Raising the grace window trades latency on genuine single-part turns; worth measuring before changing.

Also: the limiter is in-memory, so the effective limit is 10× at full HPA and resets on restart (Redis is already available); `purge_old_data.py` has no CronJob scheduling it.

## Highest-priority follow-up

**No tests were added for any of the twelve round-two fixes** — the suite is still exactly 50 tests, byte-identical to round one. `_validate_meeting_datetime`, the idempotency helpers and the privacy maskers are pure functions or thin DB helpers; they are the regression surface for everything above and would take about an hour to cover.

## Next batch

1. Tests for the round-two fixes (C7 validator, C5 idempotency, privacy maskers, auth + rate-limit interaction) — and for the round-three fixes (F2's VAD lookback window, G1's migration, F4's SQL rewrite), none of which have tests either.
2. Close VA-B1 properly — a login in front of the dashboard, or per-user credentials through the proxy instead of a shared admin key. Still the single highest-priority open item after round 3.
3. ~~One live call to confirm the Twilio signature path~~ — **done, round 4.** It failed exactly as predicted (total and silent). Four of the five round-four defects were invisible to the 50-test suite, static review and a clean boot. **Every future round should end with a live call before it is called complete.**
4. Proxy-aware rate-limit keying + exempt the probes + move the limiter to Redis.
5. Fix the `_active_calls` counter and reject duplicate live CallSid connections.
6. Per-turn LLM budget with a spoken fallback.
7. Size the DB pool against `maxReplicas`, not just per pod; schedule the retention purge.
8. Retire `backend/scripts/drop_language_columns.sql` once every environment has run the new `0002` migration (it's now marked superseded, not deleted, in case anyone's still relying on it).
8. Tests for the round-four fixes — the digit ledger is the highest-value one (`build_user_digit_stream` + `phone_supported_by_history` against a post-rolling-summary history is a pure-function test), plus a webhook test asserting the TwiML stream URL is the path form.
9. Scope or confidence-gate the LLM cache (open defect 6) — a quality regression that no crash-oriented test will ever catch.