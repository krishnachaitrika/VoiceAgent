"""
mock_interview/ — spoken practice interviews run from the dashboard.

Separate from the phone agent on purpose: nothing here is imported by the call
pipeline (voice/, brain/, orchestrator/), so a change to interview behaviour can
never affect a live customer call. It reuses the shared building blocks only —
the OpenAI client (services/providers.py), the input guardrail, Redis, and TTS.

  prompts.py  — every instruction sent to the model, as plain functions
  store.py    — session persistence (Redis, in-process fallback)
  engine.py   — the interview flow: start, answer, follow-up, skip, report
"""
