"""
mock_interview/engine.py — the interview flow.

One interview is a session dict (see _new_session) persisted in store.py after
every step. Each public function loads it, applies one step, saves it, and
returns (session, reply) where `reply` is what the browser should do next:

    {"say": "text to speak aloud",
     "prompt": {"text": "...", "label": "Question 2 · Databases", "kind": "question"},
     "done": False}

Flow per question: the candidate answers -> the model scores the answer and may
ask ONE follow-up (config.INTERVIEW_MAX_FOLLOWUPS_PER_QUESTION) -> next question.
After the last question the session is "finished" and a report can be built.

Every model call goes through _complete_json() so tests can replace that one
function instead of patching the OpenAI client.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import config
from guardrails.input_guardrail import check_input
from services.providers import get_openai_client
from mock_interview import prompts
from mock_interview.store import load_session, save_session

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")


# ── Errors ─────────────────────────────────────────────────────────────────
# Each carries the HTTP status the API layer should answer with, so
# api/interview.py maps them without knowing the engine's internals.

class InterviewError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SessionNotFound(InterviewError):
    status_code = 404


class SessionFinished(InterviewError):
    status_code = 409


class InvalidInput(InterviewError):
    status_code = 422


class ModelUnavailable(InterviewError):
    status_code = 502


# ── Model access ───────────────────────────────────────────────────────────

async def _complete_json(messages: List[dict], model: str, max_tokens: int, temperature: float) -> Tuple[dict, dict]:
    """One JSON-mode chat completion. Returns (parsed_object, usage)."""
    client = get_openai_client().with_options(timeout=config.INTERVIEW_LLM_TIMEOUT_SEC)
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("model reply was not a JSON object")
    usage = {
        "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
    }
    return data, usage


async def _ask(session: dict, messages: List[dict], model: str, max_tokens: int, temperature: float, what: str) -> dict:
    try:
        data, usage = await _complete_json(messages, model, max_tokens, temperature)
    except Exception as e:
        logger.error(f"[interview {session['id']}] {what} failed: {e}")
        raise ModelUnavailable(f"The interviewer couldn't {what} just now. Try again.") from e
    session["usage"]["prompt_tokens"] += usage["prompt_tokens"]
    session["usage"]["completion_tokens"] += usage["completion_tokens"]
    return data


# ── Pure helpers (unit-tested directly) ────────────────────────────────────

def clamp_score(value) -> Optional[int]:
    """Coerce a model-supplied score to an int 1-5, or None if it isn't one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return int(min(5, max(1, round(n))))


def recommendation_for(score: Optional[float]) -> str:
    if score is None:
        return "Not enough to judge"
    if score >= 4.3:
        return "Strong hire"
    if score >= 3.6:
        return "Hire"
    if score >= 3.0:
        return "Lean hire"
    if score >= 2.3:
        return "Lean no"
    return "No hire"


def parse_questions(data: dict, count: int) -> List[dict]:
    raw = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    questions = []
    for item in raw:
        if isinstance(item, str):
            text, topic = item, ""
        elif isinstance(item, dict):
            text, topic = item.get("text", ""), item.get("topic", "")
        else:
            continue
        text = str(text or "").strip()
        if text:
            questions.append({"text": text, "topic": str(topic or "").strip()[:40]})
    return questions[:count]


def parse_turn(data: dict) -> dict:
    kind = "clarify" if str(data.get("type", "")).strip().lower() == "clarify" else "answer"
    return {
        "type": kind,
        "score": clamp_score(data.get("score")),
        "note": str(data.get("note") or "").strip(),
        "followup": str(data.get("followup") or "").strip(),
        "say": str(data.get("say") or "").strip(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(session: dict, kind: str) -> str:
    i = session["index"]
    if kind == "followup":
        return f"Follow-up · Question {i + 1}"
    if kind == "clarify":
        return f"Question {i + 1} · Rephrased"
    topic = session["questions"][i].get("topic")
    return f"Question {i + 1}" + (f" · {topic}" if topic else "")


def _add_turn(session: dict, speaker: str, text: str, kind: str) -> None:
    session["turns"].append({
        "speaker": speaker,
        "text": text,
        "kind": kind,
        "question_index": session["index"],
        "at": _now(),
    })


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _reply(say: str, text: str, label: str, kind: str, done: bool = False) -> dict:
    return {"say": say, "prompt": {"text": text, "label": label, "kind": kind}, "done": done}


def fallback_report(session: dict, reason: str) -> dict:
    """A report built only from the per-answer notes — used when the model call
    for the full report fails, so the candidate still sees their scores."""
    questions = []
    scores = []
    for i, q in enumerate(session["questions"]):
        ev = session["evaluations"][i]
        score = ev.get("score") if ev else None
        if score is not None:
            scores.append(score)
        questions.append({
            "index": i + 1,
            "question": q["text"],
            "score": score,
            "comment": (ev.get("note") if ev else "") or ("Not reached." if ev is None else ""),
        })
    overall = round(sum(scores) / len(scores), 1) if scores else None
    return {
        "overall": overall,
        "recommendation": recommendation_for(overall),
        "summary": reason,
        "strengths": [],
        "improvements": [],
        "questions": questions,
        "source": "interviewer_notes",
        "generated_at": _now(),
    }


def parse_report(data: dict, session: dict) -> dict:
    fb = fallback_report(session, "")
    try:
        overall = float(data.get("overall"))
        overall = round(min(5.0, max(1.0, overall)), 1)
    except (TypeError, ValueError):
        overall = fb["overall"]

    rec = str(data.get("recommendation") or "").strip()
    match = next((r for r in prompts.RECOMMENDATIONS if r.lower() == rec.lower()), None)
    recommendation = match or recommendation_for(overall)

    def _list(key: str) -> List[str]:
        items = data.get(key)
        if not isinstance(items, list):
            return []
        return [str(x).strip() for x in items if str(x).strip()][:3]

    by_index = {}
    for item in data.get("questions") or []:
        if isinstance(item, dict):
            try:
                by_index[int(item.get("index"))] = item
            except (TypeError, ValueError):
                continue

    questions = []
    for q in fb["questions"]:
        item = by_index.get(q["index"])
        if item is None:
            questions.append(q)
            continue
        questions.append({
            "index": q["index"],
            "question": q["question"],
            "score": clamp_score(item.get("score")),
            "comment": str(item.get("comment") or "").strip() or q["comment"],
        })

    return {
        "overall": overall,
        "recommendation": recommendation,
        "summary": str(data.get("summary") or "").strip(),
        "strengths": _list("strengths"),
        "improvements": _list("improvements"),
        "questions": questions,
        "source": "ai",
        "generated_at": _now(),
    }


def public_view(session: dict) -> dict:
    """What the browser gets. Everything in a session is the candidate's own
    practice data, so this only reshapes it — nothing is hidden."""
    return {
        "id": session["id"],
        "role": session["role"],
        "level": session["level"],
        "focus": session["focus"],
        "status": session["status"],
        "question_index": session["index"],
        "question_count": len(session["questions"]),
        "questions": session["questions"],
        "turns": session["turns"],
        "evaluations": session["evaluations"],
        "has_report": session.get("report") is not None,
        "usage": session["usage"],
        "created_at": session["created_at"],
        "finished_at": session.get("finished_at"),
    }


# ── Session loading ────────────────────────────────────────────────────────

async def get_session(session_id: str) -> dict:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise SessionNotFound("Interview not found.")
    session = await load_session(session_id)
    if session is None:
        raise SessionNotFound("Interview not found. It may have expired — start a new one.")
    return session


async def _get_active(session_id: str) -> dict:
    session = await get_session(session_id)
    if session["status"] != "active":
        raise SessionFinished("This interview has already finished.")
    return session


# ── Flow ───────────────────────────────────────────────────────────────────

def _clean(value: str, field: str, max_len: int, required: bool = False) -> str:
    value = " ".join((value or "").split())
    if required and not value:
        raise InvalidInput(f"Enter the {field}.")
    return value[:max_len]


async def start_interview(role: str, level: str, question_count: int, focus: str = "") -> Tuple[dict, dict]:
    role = _clean(role, "role", 120, required=True)
    level = _clean(level, "experience level", 60) or "Not specified"
    focus = _clean(focus, "focus areas", 200)
    count = max(1, min(int(question_count or 5), config.INTERVIEW_MAX_QUESTIONS))

    session = _new_session(role, level, focus)
    data = await _ask(
        session,
        prompts.question_messages(role, level, count, focus),
        config.INTERVIEW_QUESTION_MODEL,
        max_tokens=1200,
        temperature=0.7,
        what="write the questions",
    )
    questions = parse_questions(data, count)
    if not questions:
        raise ModelUnavailable("The interviewer couldn't write questions for that role. Try again.")

    opening = str(data.get("opening") or "").strip() or f"Welcome. Let's begin your interview for the {role} role."
    session["questions"] = questions
    session["evaluations"] = [None] * len(questions)

    _add_turn(session, "interviewer", opening, "opening")
    first = questions[0]["text"]
    _add_turn(session, "interviewer", first, "question")
    await save_session(session)
    logger.info(f"[interview {session['id']}] Started: {len(questions)} questions for '{role}' ({level})")
    return session, _reply(_join(opening, first), first, _label(session, "question"), "question")


def _new_session(role: str, level: str, focus: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "role": role,
        "level": level,
        "focus": focus,
        "status": "active",
        "questions": [],
        "index": 0,
        "followups_asked": 0,
        "turns": [],
        "evaluations": [],
        "report": None,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "created_at": _now(),
        "finished_at": None,
    }


def _advance(session: dict, ack: str) -> dict:
    """Move to the next question, or finish if that was the last one."""
    session["index"] += 1
    session["followups_asked"] = 0
    if session["index"] >= len(session["questions"]):
        session["index"] = len(session["questions"])
        session["status"] = "finished"
        session["finished_at"] = _now()
        closing = "That was the last question. Thank you — your report is being prepared now."
        _add_turn(session, "interviewer", _join(ack, closing), "closing")
        return _reply(_join(ack, closing), "That's the end of the interview.", "Done", "done", done=True)

    question = session["questions"][session["index"]]["text"]
    _add_turn(session, "interviewer", _join(ack, question), "question")
    return _reply(_join(ack, question), question, _label(session, "question"), "question")


async def submit_answer(session_id: str, text: str) -> Tuple[dict, dict]:
    session = await _get_active(session_id)
    answer = " ".join((text or "").split())
    if not answer:
        raise InvalidInput("The answer was empty.")
    answer = answer[: config.INTERVIEW_MAX_ANSWER_CHARS]

    flagged = False
    stored = answer
    if config.ENABLE_GUARDRAILS:
        guard = check_input(f"interview {session_id}", answer)
        flagged = guard.jailbreak_detected
        stored = guard.storage_text

    # Appended in memory only. If the model call below fails the session is
    # not saved, so the browser can resend the same answer without it being
    # recorded twice.
    _add_turn(session, "candidate", stored, "answer")
    i = session["index"]
    exchange = [t for t in session["turns"] if t["question_index"] == i]
    followups_left = max(0, config.INTERVIEW_MAX_FOLLOWUPS_PER_QUESTION - session["followups_asked"])

    data = await _ask(
        session,
        prompts.turn_messages(
            session["role"], session["level"], session["questions"][i], i + 1,
            len(session["questions"]), exchange, followups_left, flagged,
        ),
        config.INTERVIEW_TURN_MODEL,
        max_tokens=300,
        temperature=0.3,
        what="respond",
    )
    decision = parse_turn(data)

    if decision["type"] == "clarify":
        rephrased = decision["say"] or session["questions"][i]["text"]
        _add_turn(session, "interviewer", rephrased, "clarify")
        reply = _reply(rephrased, rephrased, _label(session, "clarify"), "clarify")
    else:
        session["evaluations"][i] = {"score": decision["score"], "note": decision["note"]}
        if decision["followup"] and followups_left > 0:
            session["followups_asked"] += 1
            line = _join(decision["say"], decision["followup"])
            _add_turn(session, "interviewer", line, "followup")
            reply = _reply(line, decision["followup"], _label(session, "followup"), "followup")
        else:
            reply = _advance(session, decision["say"])

    await save_session(session)
    return session, reply


async def skip_question(session_id: str) -> Tuple[dict, dict]:
    session = await _get_active(session_id)
    i = session["index"]
    if session["evaluations"][i] is None:
        session["evaluations"][i] = {"score": None, "note": "Skipped."}
    _add_turn(session, "candidate", "(skipped)", "skip")
    reply = _advance(session, "Okay, let's move on.")
    await save_session(session)
    return session, reply


async def end_interview(session_id: str) -> dict:
    session = await get_session(session_id)
    if session["status"] == "active":
        session["status"] = "finished"
        session["finished_at"] = _now()
        await save_session(session)
    return session


async def build_report(session_id: str, refresh: bool = False) -> Tuple[dict, dict]:
    """Return the interview's report, generating it on first request.

    Never raises for a model failure: the candidate gets a report built from
    the per-answer notes instead, marked source="interviewer_notes", and can
    ask again with refresh=True.
    """
    session = await get_session(session_id)
    if session.get("report") and not refresh:
        return session, session["report"]

    if session["status"] == "active":
        session["status"] = "finished"
        session["finished_at"] = _now()

    answered = any(t["speaker"] == "candidate" and t["kind"] == "answer" for t in session["turns"])
    if not answered:
        report = fallback_report(session, "No answers were given, so there is nothing to score yet.")
    else:
        try:
            data = await _ask(
                session,
                prompts.report_messages(
                    session["role"], session["level"], session["questions"],
                    session["evaluations"], session["turns"],
                ),
                config.INTERVIEW_REPORT_MODEL,
                max_tokens=1400,
                temperature=0.3,
                what="write the report",
            )
            report = parse_report(data, session)
        except ModelUnavailable:
            report = fallback_report(
                session,
                "The full written report couldn't be generated, so these scores come from the "
                "interviewer's notes on each answer.",
            )

    session["report"] = report
    await save_session(session)
    return session, report
