"""
api/interview.py — HTTP surface for the browser mock interview.

    POST /api/interview/sessions                 start: writes questions, returns the first one
    GET  /api/interview/sessions/{id}            current state (e.g. after a page reload)
    POST /api/interview/sessions/{id}/answer     send one answer, get the next thing to say
    POST /api/interview/sessions/{id}/skip       skip the current question
    POST /api/interview/sessions/{id}/end        stop early
    POST /api/interview/sessions/{id}/report     build (or return) the scored report
    POST /api/interview/speak                    text -> audio in the agent's configured voice

Behind require_dashboard_auth like every other dashboard router. The browser
reaches it through the Next.js proxy (frontend/app/api/[...path]/route.js),
which only forwards requests from a signed-in dashboard session.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

import config
from auth import require_dashboard_auth
from mock_interview import engine
from voice.tts import synthesize_speech

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/interview", dependencies=[Depends(require_dashboard_auth)])


class StartRequest(BaseModel):
    role: str = Field(..., min_length=1, max_length=200)
    level: str = Field("", max_length=100)
    question_count: int = Field(5, ge=1, le=50)
    focus: str = Field("", max_length=400)


class AnswerRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


def _http(e: engine.InterviewError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/sessions")
async def start_session(body: StartRequest):
    try:
        session, reply = await engine.start_interview(body.role, body.level, body.question_count, body.focus)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session), "reply": reply}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    try:
        session = await engine.get_session(session_id)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session), "report": session.get("report")}


@router.post("/sessions/{session_id}/answer")
async def answer(session_id: str, body: AnswerRequest):
    try:
        session, reply = await engine.submit_answer(session_id, body.text)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session), "reply": reply}


@router.post("/sessions/{session_id}/skip")
async def skip(session_id: str):
    try:
        session, reply = await engine.skip_question(session_id)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session), "reply": reply}


@router.post("/sessions/{session_id}/end")
async def end(session_id: str):
    try:
        session = await engine.end_interview(session_id)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session)}


@router.post("/sessions/{session_id}/report")
async def report(session_id: str, refresh: bool = Query(False)):
    try:
        session, result = await engine.build_report(session_id, refresh=refresh)
    except engine.InterviewError as e:
        raise _http(e)
    return {"session": engine.public_view(session), "report": result}


def _audio_media_type(audio: bytes) -> str:
    """Sarvam returns WAV and ElevenLabs returns MP3 — tell the browser which."""
    if audio[:4] == b"RIFF":
        return "audio/wav"
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if audio[:4] == b"OggS":
        return "audio/ogg"
    return "application/octet-stream"


@router.post("/speak")
async def speak(body: SpeakRequest):
    """Speak interviewer text in the voice chosen on the Settings page
    (Sarvam by default, ElevenLabs when selected). 503 when no provider could
    synthesize it — the page then falls back to the browser's own voice."""
    text = " ".join(body.text.split())[: config.INTERVIEW_MAX_SPEAK_CHARS]
    audio = await synthesize_speech(text, "en-IN", sample_rate=config.INTERVIEW_TTS_SAMPLE_RATE)
    if not audio:
        raise HTTPException(status_code=503, detail="Text-to-speech is unavailable right now.")
    return Response(content=audio, media_type=_audio_media_type(audio), headers={"Cache-Control": "no-store"})
