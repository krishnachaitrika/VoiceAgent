"""
voice/stream.py — Twilio media stream handler with tuned barge-in thresholds.

KEY IMPROVEMENTS over previous version:
  1. BARGE-IN GRACE PERIOD: 0.5s → 1.2s
     Stops agent overtaking mid-sentence. Indian English has natural pauses
     after phrases like "Okay so..." — 0.5s was triggering on these.

  2. BARGE-IN THRESHOLD: 700ms → 1200ms sustained speech
     Reduces false barge-in triggers from:
       - Echo of agent's own voice through phone earpiece
       - Background noise bursts (traffic, office sounds)
       - Short acknowledgements ("mmm", "okay") that aren't real interruptions

  3. BARGE-IN RMS FACTOR: 3× → 4× silence threshold
     Acoustic echo from a handset earpiece is typically 1.5-2× silence
     threshold. 3× was too close. 4× only catches genuine loud speech.

  4. AUDIO CHUNK STREAMING: Send audio in 4KB chunks instead of one big send.
     Previously _send_audio_to_twilio sent the entire WAV as one WebSocket
     message. Twilio buffers the whole thing before playing, causing gaps
     between sentences. Chunking = Twilio starts playing while still receiving.

  5. MIN BUFFER before VAD: imported from vad.py (4000 bytes = 250ms).
     Prevents VAD from firing on the very first audio burst at call start.
"""
import asyncio
import base64
import io
import json
import logging
import re
import struct
import time
import wave
from typing import Optional

from fastapi import WebSocket
from voice.vad import (
    is_speech_ended, has_speech, calculate_rms,
    SILENCE_RMS_THRESHOLD, MIN_BUFFER_FOR_VAD
)
from voice.stt import transcribe_audio, StreamingSTTSession
from voice.tts import synthesize_speech, StreamingTTSSession
from voice.turn_state import TurnHoldState
from brain.agent import process_turn
from brain.memory import clear_session
from logging_utils import call_id_ctx, turn_id_ctx
from utils.privacy import mask_phone
import config

logger = logging.getLogger(__name__)

# ── Barge-in tuning ────────────────────────────────────────────────────────────
# These 3 constants work together to prevent the agent overtaking the caller.
#
# BARGE_IN_GRACE_PERIOD_SEC = 1.2
#   The agent ignores ALL incoming audio for the first 1.2s after it starts
#   speaking. This covers:
#     - Acoustic echo of the agent's greeting bouncing back through the handset
#     - Twilio's own audio processing latency adding a brief noise burst
#     - The caller's natural "listening start" moment where they may make a small
#       acknowledgement sound
#
# BARGE_IN_RMS_FACTOR = 4
#   Barge-in only triggers when caller's audio energy is 4× the silence threshold.
#   Previously 3×. The extra headroom filters:
#     - Residual handset echo (typically 1.5-2× silence threshold)
#     - Soft "mmm" acknowledgements while agent is still speaking
#
# BARGE_IN_DURATION_MS = 1200
#   Caller must sustain barge-in energy for 1200ms (was 700ms) before we
#   accept it as a real interruption. This means:
#     - Short noise burst (< 1200ms): ignored, agent continues
#     - Caller actually talking (> 1200ms): agent stops, caller takes turn
#
BARGE_IN_GRACE_PERIOD_SEC = 1.2
BARGE_IN_RMS_FACTOR       = 4
BARGE_IN_DURATION_MS      = 1200
BARGE_IN_THRESHOLD_BYTES  = BARGE_IN_DURATION_MS * 16  # 16 bytes/ms at 8kHz PCM16

# Audio chunk size for streaming to Twilio (4KB = ~250ms of mulaw audio)
# Smaller = lower latency but more WebSocket overhead
# Larger = higher latency but less overhead
AUDIO_CHUNK_SIZE = 4096

# Maximum buffer before forcing a turn end (20s safety cap)
MAX_BUFFER_BYTES = 320000


# ── Audio conversion ───────────────────────────────────────────────────────────

import audioop


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """Convert 8-bit mulaw (G.711) to 16-bit PCM.

    Was previously a hand-rolled byte-math G.711 decoder. Real call testing
    with the ElevenLabs Scribe STT migration surfaced that it was producing
    subtly corrupted PCM: audio that a human could still recognize as
    speech was decoded well enough for Twilio playback/VAD-threshold
    purposes, but not cleanly enough for an STT model — Scribe transcribed
    real English speech as unrelated Japanese/gibberish text rather than
    failing outright, because corrupted-but-speech-shaped PCM still "sounds
    like something" to a language model. This is the mirror-image of the
    PCM→mulaw ENCODE bug fixed earlier in this project (also hand-rolled,
    also replaced with the standard library's audioop implementation) —
    same lesson, opposite direction. audioop.ulaw2lin is the standard
    library's own G.711 mu-law decoder: battle-tested, no hand-rolled
    bit-math to get subtly wrong.
    """
    return audioop.ulaw2lin(mulaw_bytes, 2)  # 2 = output sample width in bytes (16-bit)


def pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert 16-bit PCM to 8-bit mulaw (G.711).

    Currently only reached when already_mulaw=False (see
    _send_audio_to_twilio below) — both Sarvam (SARVAM_TTS_OUTPUT_CODEC)
    and ElevenLabs (ELEVENLABS_TTS_TTS_OUTPUT_FORMAT) are configured to hand
    back audio already in mulaw, so this is dead code on the current
    default config. Replaced the hand-rolled encoder with audioop.lin2ulaw
    (standard library, battle-tested) anyway rather than leaving a
    hand-rolled G.711 implementation sitting here as a landmine for
    whenever that assumption changes — see mulaw_to_pcm() above for the
    real-world bug this exact class of hand-rolled-codec mistake caused on
    the decode side.
    """
    return audioop.lin2ulaw(pcm_bytes, 2)  # 2 = input sample width in bytes (16-bit)


def _extract_pcm_from_wav(audio_bytes: bytes) -> bytes:
    """Strip WAV header and return raw PCM frames."""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception:
        return audio_bytes


# ── Text helpers ───────────────────────────────────────────────────────────────

def split_into_sentences(text: str) -> list[str]:
    """
    Split response into natural voice chunks for pipelined TTS.

    Two-pass split:
      Pass 1: Split on sentence-ending punctuation (. ! ?)
      Pass 2: If any chunk > 120 chars, split further on commas with
              re-merging to keep chunks between 40-120 chars

    Result: each chunk is a natural breathing unit for the TTS voice.
    """
    raw = re.split(r'(?<=[.!?])\s+', text.strip())
    result = []
    for sentence in raw:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > 120:
            parts = re.split(r',\s+', sentence)
            current = ""
            for part in parts:
                candidate = (current + ", " + part).strip(", ") if current else part
                if len(candidate) > 100 and current:
                    result.append(current)
                    current = part
                else:
                    current = candidate
            if current:
                result.append(current)
        else:
            result.append(sentence)
    return [s for s in result if len(s.strip()) > 2]


def deduplicate_transcript(text: str) -> str:
    """Remove repeated sentences and word-level duplicates from STT output."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    seen, unique = [], []
    for s in sentences:
        norm = s.strip().lower().rstrip('.')
        if norm not in seen:
            seen.append(norm)
            unique.append(s.strip())
    result = ' '.join(unique)
    words = result.split()
    if len(words) >= 2:
        deduped = [words[0]]
        for w in words[1:]:
            if w.lower() != deduped[-1].lower():
                deduped.append(w)
        result = ' '.join(deduped)
    return result


def is_filler_transcript(text: str) -> bool:
    """
    Filter STT hallucinations and meaningless fillers.

    Filters single words and two-word combinations that are known
    STT hallucinations on near-silent Indian phone audio.
    Does NOT filter short but meaningful phrases like "Book a meeting".

    REAL-BUG FIX: this used to lump TRUE_FILLERS (interjections with no
    semantic content — "um", "hmm", "uh") together with words that are
    ALWAYS a real, meaningful answer ("yes", "no", "okay", "sure") in one
    droppable set. That was wrong: a real call confirmed a caller saying
    just "Yeah." in direct answer to the agent's "Is everything correct?"
    got silently dropped as filler — the agent then just sat there, and the
    caller had to repeat themselves as a longer sentence before it
    registered. "Um"/"hmm" genuinely carry zero information regardless of
    context and are safe to always drop; "yes"/"no"/"okay" etc. are a
    complete, real answer whenever they're said and must always reach the
    orchestrator so it can use conversation context to know what they're
    answering — the LLM already has that context, this function does not,
    so it should never be the one deciding a real answer doesn't matter.
    """
    TRUE_FILLERS = {
        "hmm", "um", "uh", "ah", "oh", "huh", "mm", "mmm",
    }
    # Meaningful short answers — NEVER dropped, no matter how short, since
    # each one is always a complete real answer, never just noise. Listed
    # explicitly (not just "not in TRUE_FILLERS") because the length<3
    # catch-all below would otherwise still eat "no"/"ok" purely for being
    # under 3 characters — a second, separate instance of the same real
    # bug this function is being fixed for.
    SHORT_MEANINGFUL_ANSWERS = {
        "yes", "no", "ok", "okay", "yeah", "yep", "nope", "sure",
        "right", "alright", "fine", "correct",
    }
    cleaned = re.sub(r'[^\w\s]', '', text.strip().lower())
    words = cleaned.split()
    if len(words) == 1 and words[0] in SHORT_MEANINGFUL_ANSWERS:
        return False
    if len(cleaned) < 3:
        return True
    if len(words) == 1 and words[0] in TRUE_FILLERS:
        return True
    if len(words) == 2 and all(w in TRUE_FILLERS for w in words):
        return True
    return False


# REAL-BUG FIX (confirmed via two separate real calls, on fresh/non-cached
# LLM calls both times): a prompt-level instruction telling the LLM not to
# treat "Hi hello, <real question>" as a connectivity check was NOT
# reliable enough on its own — the LLM kept answering real questions with
# the canned "Yes, I can hear you — go ahead!" purely because the message
# started with repeated greeting words, even with an explicit rule and
# concrete negative examples in the prompt. Since LLM instruction-following
# can't be guaranteed the way code can, this closes the gap deterministically
# instead: strip a leading run of standalone greeting words before the LLM
# ever sees the transcript, so the confusing pattern (greeting immediately
# followed by real content) simply never reaches the model. Only affects
# what's sent to the LLM — the original transcript is still what gets
# logged/stored/shown on the dashboard, so nothing about call records
# changes.
#
# REAL-BUG FIX, PART 2 (confirmed via a THIRD real call): the leading-only
# version above still wasn't enough. The caller said "Okay, hello. Can you
# explain about the service now field?" — "hello" is the SECOND word here
# (after the filler "Okay"), not the first, so the old leading-only scan
# stopped immediately at word 0 (since "okay" isn't a greeting) and never
# touched the "hello" sitting right after it. The LLM still saw a "hello"
# near the start and misfired the same way again. Rewritten to remove
# standalone greeting words WHEREVER they appear in the message, not just
# a leading run — this is what the LLM was actually reacting to, position
# in the sentence was never really the trigger. Real content elsewhere in
# the message (a name, a real word containing "hi" as a substring, etc.)
# is untouched — this only matches whole-word "hi"/"hello"/"hey"/etc.,
# never mid-word.
_LEADING_GREETINGS = {"hi", "hello", "hey", "hiya", "yo"}
_GREETING_WORD_RE = re.compile(
    r'\b(?:' + '|'.join(_LEADING_GREETINGS) + r')\b[.,!?]*',
    re.IGNORECASE,
)


def strip_leading_greeting(text: str) -> str:
    """
    Remove standalone greeting words ("Hi", "hello", "hey", ...) anywhere
    in a transcript before it reaches the LLM, but ONLY when real content
    remains afterward — a message that is only greetings is returned
    unchanged, since that case is handled separately (is_filler_transcript
    drops it entirely if it's 1-2 words, and the system prompt's "caller
    said only hello" rule covers anything longer that's still just
    greetings repeated). Name kept as strip_leading_greeting for callers
    already using it, even though it no longer strips only leading ones —
    see the REAL-BUG FIX PART 2 comment above for why the scope had to
    widen.
    """
    cleaned = _GREETING_WORD_RE.sub('', text)
    cleaned = re.sub(r'^[\s,.!?]+', '', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned if cleaned else text  # empty means it was ALL greetings


# ── Smart hearing (config.ENABLE_SMART_HEARING) ─────────────────────────────
# Trailing words that mean a sentence almost certainly isn't finished yet —
# conjunctions, prepositions, articles, and similar words a real sentence
# doesn't normally end on. This is the "Does it end in something that
# sounds incomplete?" half of the original plan.
_DANGLING_TRAILING_WORDS = {
    "and", "but", "so", "because", "to", "for", "or", "with", "of", "in",
    "on", "at", "the", "a", "an", "is", "are", "was", "were", "that",
    "which", "if", "when", "can", "could", "would", "should", "will",
    "do", "does", "did", "i", "you", "we", "they", "he", "she", "it",
    "my", "your", "our", "their", "about", "from", "as", "than", "than",
}


def sounds_incomplete(text: str) -> bool:
    """
    Heuristic completeness check — does this transcript sound like a
    finished thought, or like Sarvam's VAD cut it off mid-sentence?

    Three signals, matching the original plan exactly:
      - trails off on a dangling word (and/but/so/because/to/for/...)
      - no terminal punctuation (. ? !)
      - very short (<=config.SMART_HEARING_SHORT_FRAGMENT_WORDS words) with
        no punctuation — "Yeah, can you"

    This is deliberately conservative (a plain acoustic-silence VAD has zero
    grammatical awareness, so this only needs to catch the obvious cases) —
    see config.ENABLE_SMART_HEARING's docstring for the real-call evidence
    this is meant to fix.
    """
    t = text.strip()
    if not t:
        return False
    if t[-1] in ".?!":
        return False
    words = t.split()
    if not words:
        return False
    last_word = words[-1].lower().strip(",;:\"'")
    if last_word in _DANGLING_TRAILING_WORDS:
        return True
    if len(words) <= config.SMART_HEARING_SHORT_FRAGMENT_WORDS:
        return True
    return False


# ── Twilio audio sender ────────────────────────────────────────────────────────

async def _send_audio_to_twilio(
    websocket: WebSocket,
    audio_bytes: bytes,
    stream_sid: str,
    already_mulaw: bool = False,
) -> None:
    """
    Send audio to Twilio in chunks for lower perceived latency.

    Previously sent the entire WAV as one WebSocket message. Twilio buffers
    the whole payload before playing, which means:
      - A 4s TTS response causes 4s of silence then audio all at once
    
    Chunking sends 4KB at a time (~250ms of audio). Twilio begins playing
    the first chunk while still receiving the rest — the caller hears audio
    starting much sooner.

    already_mulaw=True skips the WAV-extract + PCM->mulaw conversion below.
    The streaming TTS path (voice/tts.py: StreamingTTSSession, config.
    SARVAM_TTS_OUTPUT_CODEC="mulaw") asks Sarvam to hand back audio already
    in Twilio's native 8kHz mu-law format, so re-decoding/re-encoding it
    here would be redundant work on the hot path — the batch TTS path
    (voice/tts.py: synthesize_speech) still returns a WAV file, so it keeps
    already_mulaw=False (the default) and goes through the conversion.
    """
    from starlette.websockets import WebSocketState
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        if already_mulaw:
            mulaw_bytes = audio_bytes
        else:
            pcm_bytes   = _extract_pcm_from_wav(audio_bytes)
            mulaw_bytes = pcm_to_mulaw(pcm_bytes)

        # Send in chunks
        for i in range(0, len(mulaw_bytes), AUDIO_CHUNK_SIZE):
            chunk   = mulaw_bytes[i:i + AUDIO_CHUNK_SIZE]
            payload = base64.b64encode(chunk).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event":     "media",
                "streamSid": stream_sid,
                "media":     {"payload": payload},
            }))
            # Yield control briefly so the receive loop can process barge-in
            await asyncio.sleep(0)

    except Exception as e:
        logger.error(f"Failed to send audio chunk to Twilio: {e}")


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_twilio_stream(
    websocket: WebSocket,
    call_id: str,
    on_call_start=None,
    on_call_end=None,
) -> None:
    """
    Enterprise Twilio media stream handler.

    Conversation flow per turn:
      Caller speaks → VAD detects end of speech (1.5s silence)
      → STT (ElevenLabs, ~1.5s) → dedup + filler filter
      → PARALLEL: (GPT-4o intent) + (RAG pre-fetch) → tool execution
      → Split response into sentences
      → Pipeline TTS: pre-fetch sentence N+1 while sentence N plays
      → Stream each sentence in 4KB chunks to Twilio

    Barge-in:
      While agent speaks, monitor caller audio.
      Grace period 1.2s → then require 1200ms of RMS > 4× threshold
      → cancel current turn → clear Twilio buffer → start new turn
    """
    await websocket.accept()
    # Bind call_id to every log line emitted anywhere during this call (see
    # logging_utils.py) — including from brain/orchestrator/rag, which never
    # see call_id as an argument. Reset in the outer finally block below.
    call_id_ctx_token = call_id_ctx.set(call_id)
    logger.info(f"[{call_id}] WebSocket accepted")

    audio_buffer: bytes       = b""
    # English-only build: the agent no longer detects or switches language
    # mid-call — this is always "en-IN" (see _map_to_bcp47 in voice/stt.py).
    # Kept as a named constant (rather than inlining the literal at every
    # call site below) so nothing else in this function needs to change.
    language_detected: str    = "en-IN"
    stream_sid: Optional[str] = None
    call_start_time: float    = time.time()
    total_openai_cost: float  = 0.0
    # VA-T-010 — spend on turns the caller never heard (barge-in, or a newer
    # turn superseding this one). Counted inside total_openai_cost because
    # OpenAI billed it; tracked separately because it is the one part of the
    # bill that tuning can actually reduce.
    total_discarded_cost: float = 0.0
    discarded_turn_count: int   = 0
    total_sarvam_cost: float  = 0.0

    # REAL-BUG FIX: previously never populated anywhere — every Call record
    # in the database was permanently stored as phone_number="unknown".
    # Twilio's Media Streams "start" event carries this in
    # start.customParameters, ONLY because api/twilio_webhook.py now
    # explicitly attaches it via <Parameter name="from" .../> (Twilio does
    # not include the caller's number in the stream payload on its own).
    # Defaults to "unknown" so a call still records cleanly if this ever
    # comes through empty (e.g. an internal test call placed with a raw
    # WebSocket client that skips the TwiML step entirely).
    caller_phone_number: str  = "unknown"
    # "inbound" or "outbound" — arrives via customParameters like the caller
    # number does, because the media-stream WebSocket carries no Twilio
    # metadata of its own. Used by api/websocket.py to price Twilio minutes,
    # which differ by an order of magnitude between the two.
    call_direction: str       = "inbound"

    # Smart hearing / adaptive multi-part merging — the decision logic for
    # both lives in voice/turn_state.py (TurnHoldState) so it can be
    # unit-tested with an injectable clock; see that module's docstring for
    # the real-call evidence each behaviour guards against. (Sustained
    # language-switch confirmation used to live here too — removed along
    # with all language-switching below; English-only build.)
    # pending_finalize_task (the grace-period timer that finalizes a held
    # fragment if the caller doesn't keep talking) stays here since it's
    # asyncio orchestration, not a state-machine decision.
    turn_hold = TurnHoldState()
    pending_finalize_task: Optional[asyncio.Task] = None

    turns: list               = []

    agent_speaking: bool         = False
    agent_speech_start: float    = 0.0
    barge_in_speech_bytes: int   = 0

    BARGE_IN_RMS_THRESHOLD = SILENCE_RMS_THRESHOLD * BARGE_IN_RMS_FACTOR

    current_turn_task: Optional[asyncio.Task] = None
    pending_stt_tasks: list                   = []
    greeting_sent: bool                       = False

    # ── v4 streaming session state (config.ENABLE_STREAMING_STT / _TTS) ────
    # stt_session: one ElevenLabs streaming-STT WebSocket kept open for the
    #   whole call (see voice/stt.py: StreamingSTTSession) — only used when
    #   config.ENABLE_STREAMING_STT is true.
    # current_tts_session: the streaming-TTS WebSocket for whichever turn is
    #   currently speaking (see voice/tts.py: StreamingTTSSession) — only
    #   used when config.ENABLE_STREAMING_TTS is true. Tracked here (not
    #   just as a local inside run_turn) so the barge-in path can close it
    #   immediately per Sarvam's "no in-band cancel" guidance.
    stt_session: Optional[StreamingSTTSession] = None
    current_tts_session: Optional[StreamingTTSSession] = None

    # ── STT resilience state (config.STT_RECONNECT_*) ──────────────────────
    # stt_reconnecting: guards against overlapping reconnect sequences if
    #   more than one in-flight send happens to fail around the same time.
    # stt_permanently_failed: set once every reconnect attempt has been
    #   exhausted for this call — media chunks are dropped from then on
    #   instead of silently piling into a dead session, and the spoken
    #   fallback + escalation only ever fires once per call.
    stt_reconnecting: bool = False
    stt_permanently_failed: bool = False
    # True once the streaming STT session has connected successfully at
    # least once this call. Lets the media handler below tell "never
    # connected at all" (call-start failure — the existing, intentional
    # fall-through to the batch STT path for the whole call) apart from "was
    # connected, died mid-call, currently reconnecting" (must NOT silently
    # fall through to batch mid-call — that pipeline was never initialized
    # for this call and the caller has already been told about the issue).
    stt_ever_connected: bool = False

    # Turn versioning — prevents a superseded turn from ever sending stale
    # audio to Twilio. Every user turn gets a strictly increasing id; a turn
    # is only allowed to speak while it is still the "active" one.
    turn_id_counter: int = 0
    active_turn_id: int  = 0

    # current_turn_response_ready: set True by run_turn the instant its
    # response text comes back from the LLM/RAG (before any TTS/audio),
    # False whenever a new turn starts. Lets handle_user_turn tell "the
    # previous turn is still just thinking (nothing lost if cancelled)"
    # apart from "the previous turn already has a real answer ready and is
    # on the verge of speaking it" — see config.TURN_PREEMPT_GRACE_MS.
    current_turn_response_ready: bool = False

    # ── TTS with retry ────────────────────────────────────────────────────────

    async def tts_with_retry(sentence: str, lang: str) -> Optional[bytes]:
        for attempt in range(2):
            try:
                return await synthesize_speech(sentence, lang)
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"[{call_id}] TTS retry: {e}")
                    await asyncio.sleep(0.2)
                else:
                    logger.error(f"[{call_id}] TTS failed: {e}")
        return None

    async def _close_current_tts_session() -> None:
        nonlocal current_tts_session
        if current_tts_session is not None:
            session_to_close = current_tts_session
            current_tts_session = None
            try:
                await session_to_close.close()
            except Exception as e:
                logger.warning(f"[{call_id}] Error closing streaming TTS session: {e}")

    async def _trigger_barge_in(reason: str) -> None:
        """Shared barge-in path for both the legacy RMS-based detector and
        the ElevenLabs streaming-STT speech-start signal below — cancels the
        in-flight turn, clears whatever Twilio already has queued, and
        closes any open streaming-TTS socket (no in-band cancel exists for
        it, per Sarvam's TTS docs — closing + reconnecting next turn is the
        documented approach)."""
        nonlocal agent_speaking, barge_in_speech_bytes, active_turn_id, audio_buffer
        logger.info(f"[{call_id}] Barge-in detected ({reason}) — stopping agent")
        active_turn_id = -1
        # A real barge-in is direct proof of multi-part intent: the caller
        # had more to say and didn't wait for the reply to finish. Since
        # we're already forced to stop and listen, there's no extra cost in
        # also giving the next fragment a brief grace window (see
        # config.ENABLE_MULTI_PART_MERGE) instead of firing a lone answer
        # for just the interruption.
        if config.ENABLE_MULTI_PART_MERGE:
            if turn_hold.register_multi_part_signal_from_bargein(config.MULTI_PART_TRIGGER_COUNT):
                logger.info(f"[{call_id}] Multi-part speech pattern confirmed via barge-in — "
                            f"enabling adaptive turn merging for rest of call")
        if current_turn_task and not current_turn_task.done():
            current_turn_task.cancel()
            # Wait for the cancelled task's OWN finally block (in
            # _speak_via_streaming_tts) to actually finish tearing down its
            # TTS session before we independently touch it below — cancel()
            # only schedules the cancellation, it doesn't complete it
            # synchronously. Closing the same websocket generator from two
            # places concurrently is exactly what caused a real
            # "asynchronous generator is already running" error in testing.
            try:
                await current_turn_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[{call_id}] Cancelled turn task raised on cleanup: {e}")
        await _close_current_tts_session()
        if stream_sid:
            try:
                await websocket.send_text(json.dumps({
                    "event":     "clear",
                    "streamSid": stream_sid,
                }))
            except Exception as e:
                logger.warning(f"[{call_id}] Failed to clear Twilio buffer: {e}")
        agent_speaking        = False
        barge_in_speech_bytes = 0
        audio_buffer          = b""

    async def _speak_via_streaming_tts(
        sentences: list,
        lang: str,
        is_stale_fn,
        label: str,
    ) -> None:
        """
        Shared streaming-TTS turn lifecycle, used by both run_turn (the
        agent's replies) and the one-time greeting below — connect, feed
        every sentence, forward audio to Twilio as it streams back, close.

        Two failure modes this specifically guards against, learned from a
        real stuck-call bug: (1) if feeding text raises (bad config, closed
        socket, whatever), that exception must be logged, not silently
        swallowed in a fire-and-forget task — a swallowed feed exception
        left the caller listening to nothing with zero error output; (2) if
        Sarvam's socket ever goes quiet (no audio, no close, no error), the
        whole thing is wrapped in config.STREAMING_TTS_TURN_TIMEOUT_SEC so
        the call can never hang forever waiting on it.

        is_stale_fn() -> bool: for run_turn this checks turn versioning; for
        the greeting it's just `lambda: False` since there's no prior turn
        to be superseded by.
        """
        nonlocal current_tts_session, total_sarvam_cost
        tts_session = StreamingTTSSession(call_id, lang)
        current_tts_session = tts_session

        async def _body() -> None:
            await tts_session.connect()

            async def _feed() -> None:
                for i, sentence in enumerate(sentences):
                    if not agent_speaking or is_stale_fn():
                        logger.info(f"[{call_id}] {label}: stopped feeding at sentence {i+1}")
                        break
                    await tts_session.send_text(sentence)
                await tts_session.finish()

            feed_task = asyncio.create_task(_feed())
            try:
                async for audio_bytes, _content_type in tts_session.audio_chunks():
                    if not agent_speaking or is_stale_fn():
                        break
                    if stream_sid:
                        await _send_audio_to_twilio(
                            websocket, audio_bytes, stream_sid, already_mulaw=True
                        )
            finally:
                # Always retrieve the feed task's result — even on the
                # normal-completion path — so a raised exception surfaces
                # in the log instead of vanishing into an unretrieved task.
                if not feed_task.done():
                    feed_task.cancel()
                try:
                    await feed_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"[{call_id}] {label}: streaming TTS feed failed: {e}")

        try:
            await asyncio.wait_for(_body(), timeout=config.STREAMING_TTS_TURN_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.error(
                f"[{call_id}] {label}: streaming TTS turn exceeded "
                f"{config.STREAMING_TTS_TURN_TIMEOUT_SEC}s with no audio/close from "
                f"Sarvam — aborting this turn's speech instead of hanging the call."
            )
        except Exception as e:
            logger.error(f"[{call_id}] {label}: streaming TTS turn failed: {e}")
        finally:
            if current_tts_session is tts_session:
                current_tts_session = None
            # VA-C2 fix: this session's characters_sent is the only signal
            # needed to cost this turn's speech — read it before close()
            # discards the session, regardless of how this turn ended
            # (normal completion, barge-in, timeout, or feed failure above
            # all reach this finally).
            total_sarvam_cost += tts_session.cost_usd()
            await tts_session.close()

    # ── One full agent turn ───────────────────────────────────────────────────

    async def run_turn(user_text: str, lang: str, my_turn_id: int) -> None:
        nonlocal agent_speaking, agent_speech_start, total_openai_cost, current_turn_response_ready

        def _is_stale() -> bool:
            # True once a newer user turn has taken over — this turn must
            # never speak again, even if it's mid-flight past a cancel().
            return my_turn_id != active_turn_id

        # run_turn is always its own asyncio Task, which gets its own copy
        # of the current context — this binding never leaks into a
        # concurrently-running sibling turn.
        turn_id_ctx_token = turn_id_ctx.set(str(my_turn_id))

        try:
            # Agent reasoning (parallel RAG inside agent.py)
            # strip_leading_greeting: see its docstring above — real,
            # code-level fix for the confirmed-recurring "Hi hello, <real
            # question>" -> wrongly answered "Yes, I can hear you" bug.
            # user_text here is ONLY what reaches the LLM; the original
            # transcript (with the greeting intact) is what's already been
            # logged/stored by the caller of run_turn.
            llm_input = strip_leading_greeting(user_text)
            response_text, turn_cost = await process_turn(call_id, llm_input, lang)
            total_openai_cost += turn_cost

            if _is_stale():
                # VA-T-010 — THE COST IS REAL, SO IT STAYS COUNTED.
                #
                # This turn's answer is discarded (the caller interrupted, or a
                # newer turn superseded it), but OpenAI already generated and
                # billed it. Subtracting it here would make the dashboard
                # understate what the call actually cost — reporting less than
                # the invoice, which is the wrong direction for a finance
                # figure to be wrong in.
                #
                # So it is counted AND tracked separately. Discarded spend is a
                # real, reducible number: it is the price of the barge-in and
                # multi-part-merge tuning, and if it climbs, the grace windows
                # in config.py are the lever. You cannot tune what you cannot
                # see, and previously this was invisible.
                nonlocal total_discarded_cost, discarded_turn_count
                total_discarded_cost += turn_cost
                discarded_turn_count += 1
                logger.info(
                    f"[{call_id}] Turn {my_turn_id} superseded before speaking — "
                    f"discarding reply (${turn_cost:.5f} already spent; "
                    f"{discarded_turn_count} discarded so far this call)"
                )
                return

            logger.info(f"[{call_id}] Agent: {response_text}")
            turns.append({"role": "assistant", "content": response_text})

            # From here on, this turn has a real, fully-generated answer —
            # discarding it from this point on would mean the caller never
            # hears something they've already effectively "received" from
            # the agent's side. handle_user_turn checks this flag before
            # deciding whether a new incoming turn may preempt it outright.
            current_turn_response_ready = True

            sentences = split_into_sentences(response_text)
            if not sentences:
                sentences = [response_text]

            agent_speaking    = True
            agent_speech_start = time.time()

            if config.ENABLE_STREAMING_TTS:
                # One Sarvam TTS WebSocket for this whole turn: sentences
                # are fed in as they're ready (today, that's still after
                # split_into_sentences() on the full GPT reply — GPT-level
                # token streaming is the next phase; the win here is that
                # each sentence's audio starts reaching Twilio as Sarvam
                # synthesizes it, instead of waiting for that sentence's
                # entire clip before sending anything). See
                # _speak_via_streaming_tts for the timeout/error-surfacing
                # guarantees this relies on.
                await _speak_via_streaming_tts(sentences, lang, _is_stale, "Barge-in")

            else:
                # ── Batch TTS path (existing behaviour) ─────────────────
                # Pre-fetch first sentence
                prefetch: Optional[asyncio.Task] = asyncio.create_task(
                    tts_with_retry(sentences[0], lang)
                )

                for i, sentence in enumerate(sentences):
                    if not agent_speaking or _is_stale():
                        if prefetch and not prefetch.done():
                            prefetch.cancel()
                        logger.info(f"[{call_id}] Barge-in: stopped at sentence {i+1}")
                        break

                    # Await pre-fetched audio
                    try:
                        audio = await prefetch
                    except (asyncio.CancelledError, Exception) as e:
                        logger.warning(f"[{call_id}] Pre-fetch error: {e}")
                        audio = None
                        break

                    # Pre-fetch next sentence immediately
                    next_i = i + 1
                    if next_i < len(sentences) and agent_speaking and not _is_stale():
                        prefetch = asyncio.create_task(
                            tts_with_retry(sentences[next_i], lang)
                        )
                    else:
                        prefetch = None

                    # Send current sentence to Twilio in chunks — re-check right
                    # before the send since this is the only place audio actually
                    # reaches the caller's ear.
                    if audio and stream_sid and agent_speaking and not _is_stale():
                        await _send_audio_to_twilio(websocket, audio, stream_sid)

        except asyncio.CancelledError:
            logger.info(f"[{call_id}] Turn {my_turn_id} cancelled (barge-in or superseded)")
            raise
        finally:
            # Only the still-active turn is allowed to clear the speaking
            # flag — otherwise a late-cancelled old turn could stomp on a
            # newer turn that has already started speaking.
            if not _is_stale():
                agent_speaking = False
            turn_id_ctx.reset(turn_id_ctx_token)

    # ── Start new turn, cancel any in-progress ────────────────────────────────

    async def handle_user_turn(transcript: str, lang: str) -> None:
        nonlocal current_turn_task, turn_id_counter, active_turn_id, agent_speaking
        nonlocal current_turn_response_ready

        # ── Adaptive multi-part detection: "quick succession" signal ───────
        # If the caller started talking again suspiciously soon after the
        # PREVIOUS turn was dispatched, that's evidence they're delivering
        # several points in quick bursts rather than one-at-a-time — even
        # though (by construction) this current turn is being dispatched
        # right now on its own. Once observed, this flips multi_part_mode
        # on for the rest of the call, so future fragments get a brief
        # grace window instead of being answered the instant they sound
        # grammatically complete. See config.ENABLE_MULTI_PART_MERGE.
        if config.ENABLE_MULTI_PART_MERGE and not turn_hold.multi_part_mode:
            gap_ms = turn_hold.quick_succession_gap_ms()
            if gap_ms is not None and 0 <= gap_ms < config.MULTI_PART_QUICK_SUCCESSION_MS:
                if turn_hold.register_multi_part_signal_from_gap(config.MULTI_PART_TRIGGER_COUNT):
                    logger.info(
                        f"[{call_id}] Multi-part speech pattern confirmed via quick-succession "
                        f"gap ({gap_ms:.0f}ms) — enabling adaptive turn merging for rest of call"
                    )
        turn_hold.mark_turn_dispatched()

        # If the current turn already has a ready, real answer and hasn't
        # finished delivering it, give it a bounded head start BEFORE
        # touching turn/active ids at all. Bumping active_turn_id makes
        # the running turn see itself as stale on its very next check and
        # stop sending audio — so this has to happen first, or the grace
        # window below would silently do nothing.
        # REAL-BUG FIX (confirmed via real call logs): this grace period used
        # to only apply `and current_turn_response_ready` — i.e. only once
        # the in-flight turn already had a fully-generated answer. If a
        # second question arrived WHILE the first was still inside
        # process_turn() (GPT/RAG still running, no answer yet), that
        # condition was False, this whole grace-wait was skipped, and the
        # cancel() a few lines below fired immediately — killing the first
        # question's in-flight GPT/RAG call before it ever produced an
        # answer. The caller experienced this as "I asked something, then
        # asked something else before it replied, and the first thing I
        # asked was never answered." Removing the current_turn_response_ready
        # condition means ANY in-flight turn — whether still generating or
        # already speaking — gets this bounded head start before being
        # superseded, not just ones lucky enough to have already finished.
        if current_turn_task and not current_turn_task.done():
            deadline = time.time() + (config.TURN_PREEMPT_GRACE_MS / 1000)
            while not current_turn_task.done() and time.time() < deadline:
                await asyncio.sleep(0.05)

        # Bump the turn id now so any still-in-flight run_turn immediately
        # sees itself as stale, even before its cancellation is scheduled.
        turn_id_counter += 1
        this_turn_id  = turn_id_counter
        active_turn_id = this_turn_id

        if current_turn_task and not current_turn_task.done():
            current_turn_task.cancel()
            try:
                # Wait for the old turn to actually unwind before we touch
                # Twilio again. Without this, the old turn can still be
                # mid-send when the new turn's audio starts — Twilio then
                # plays both replies back to back, which is the "reply
                # comes twice" bug.
                await current_turn_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[{call_id}] Previous turn raised on cancel: {e}")

            # Flush whatever partial audio Twilio already has queued from
            # the superseded turn so it can never bleed into the new reply.
            if stream_sid:
                try:
                    await websocket.send_text(json.dumps({
                        "event":     "clear",
                        "streamSid": stream_sid,
                    }))
                except Exception as e:
                    logger.warning(f"[{call_id}] Failed to clear Twilio buffer: {e}")

            agent_speaking = False

        logger.info(f"[{call_id}] User said: {transcript}")
        turns.append({"role": "user", "content": transcript})
        current_turn_response_ready = False
        current_turn_task = asyncio.create_task(run_turn(transcript, lang, this_turn_id))

    # ── STT + quality pipeline ────────────────────────────────────────────────

    async def process_user_speech(audio_snapshot: bytes, lang: str) -> None:
        nonlocal language_detected
        transcript, detected_lang = await transcribe_audio(audio_snapshot, lang)
        language_detected = detected_lang
        if not transcript.strip():
            return
        transcript = deduplicate_transcript(transcript)
        if not transcript.strip():
            return
        # Runs through the same merge/hold pipeline as the streaming STT
        # path below, so a caller who pauses mid-thought (or fires off a
        # multi-part burst) gets identical handling regardless of which
        # STT path is active for this call.
        combined = await _combine_with_pending(transcript)
        await _finalize_or_hold(combined, detected_lang)

    # ── v4 streaming STT callbacks (config.ENABLE_STREAMING_STT) ─────────────
    # ElevenLabs' streaming session (voice/stt.py: StreamingSTTSession) does its
    # own VAD and hands back a transcript per completed utterance directly —
    # no client-side audio_buffer/is_speech_ended accumulation needed, so
    # this is a much shorter pipeline than process_user_speech above. It
    # still runs the same dedup + filler-check quality gate before handing
    # off to handle_user_turn, so STT hallucination filtering behaves
    # identically regardless of which STT path is active.

    async def _finalize_pending_fragment(reason: str) -> None:
        """Send whatever's currently held as a real turn, regardless of
        whether it still looks incomplete/whether adaptive mode is on —
        called either because the grace period elapsed with no
        continuation, or because SMART_HEARING_MAX_HOLD_MS was hit. Never
        holds forever."""
        nonlocal pending_finalize_task
        if not turn_hold.pending_fragment:
            return
        text_to_send = turn_hold.pending_fragment
        turn_hold.reset_pending()
        pending_finalize_task = None
        logger.info(f"[{call_id}] Finalizing held fragment ({reason}): '{text_to_send}'")
        await handle_user_turn(text_to_send, language_detected)

    async def _grace_period_expired(grace_ms: int) -> None:
        try:
            await asyncio.sleep(grace_ms / 1000)
        except asyncio.CancelledError:
            return
        await _finalize_pending_fragment("grace period elapsed, no continuation")

    async def _combine_with_pending(new_text: str) -> str:
        """Merge new_text with whatever fragment is currently being held
        (if any), and cancel any in-flight grace-period timer since we now
        have new information to fold in. Shared by both the batch VAD path
        (process_user_speech) and the streaming STT path
        (_on_stt_transcript) so a fragment held by either can be completed
        by the other — they're never really separate pipelines from the
        caller's point of view."""
        nonlocal pending_finalize_task
        combined = turn_hold.combine_with_pending(new_text)

        if pending_finalize_task and not pending_finalize_task.done():
            pending_finalize_task.cancel()
        pending_finalize_task = None
        return combined

    async def _finalize_or_hold(combined: str, lang: str) -> None:
        """Single decision point, shared by both STT paths: is `combined`
        ready to answer now, or should it be held a little longer in case
        the caller keeps talking? Three independent reasons to hold, each
        with its own grace window (the longest applicable one wins if more
        than one applies):
          1. It's a short clause-starter with no terminal punctuation
             ("Could you", "Can you also") — real one-word acknowledgements
             are already dropped by is_filler_transcript() before this
             point, so what's left here is overwhelmingly a caller paused
             mid-thought, and gets the most patience
             (config.SMART_HEARING_SHORT_FRAGMENT_GRACE_MS).
          2. It otherwise sounds grammatically incomplete, e.g. trails off
             on "and"/"so"/"to" (config.ENABLE_SMART_HEARING).
          3. This call has already shown a pattern of multi-part bursts
             (config.ENABLE_MULTI_PART_MERGE / multi_part_mode) — so even a
             complete-sounding sentence might just be point 1 of several
             the caller intends to make before waiting for a reply.
        Either way, SMART_HEARING_MAX_HOLD_MS is a hard ceiling — a caller
        who never stops adding to it still gets answered eventually."""
        nonlocal pending_finalize_task

        if is_filler_transcript(combined):
            logger.info(f"[{call_id}] Dropped filler: '{combined}'")
            turn_hold.reset_pending()
            return

        decision = turn_hold.decide_hold(
            combined,
            smart_hearing_enabled=config.ENABLE_SMART_HEARING,
            multi_part_merge_enabled=config.ENABLE_MULTI_PART_MERGE,
            short_fragment_words=config.SMART_HEARING_SHORT_FRAGMENT_WORDS,
            short_fragment_grace_ms=config.SMART_HEARING_SHORT_FRAGMENT_GRACE_MS,
            smart_hearing_grace_ms=config.SMART_HEARING_GRACE_MS,
            multi_part_hold_grace_ms=config.MULTI_PART_HOLD_GRACE_MS,
            max_hold_ms=config.SMART_HEARING_MAX_HOLD_MS,
            sounds_incomplete_fn=sounds_incomplete,
        )

        if decision.hold:
            turn_hold.pending_fragment = combined
            logger.info(
                f"[{call_id}] Holding fragment ({decision.reason}, {decision.grace_ms}ms grace): '{combined}'"
            )
            pending_finalize_task = asyncio.create_task(_grace_period_expired(decision.grace_ms))
            return

        # Sounds complete, no adaptive reason to hold, or we've held it
        # long enough already — send it through as one turn.
        turn_hold.reset_pending()
        await handle_user_turn(combined, lang)

    async def _on_stt_transcript(
        transcript: str, detected_lang: Optional[str], lang_probability: Optional[float]
    ) -> None:
        # English-only build: STT is forced to English at the source (see
        # voice/stt.py), so there's no detected-language switching to gate
        # here any more — detected_lang/lang_probability are accepted for
        # signature compatibility with StreamingSTTSession's on_transcript
        # callback but no longer used.
        nonlocal pending_finalize_task

        transcript = deduplicate_transcript(transcript)
        if not transcript.strip():
            return

        # Merge with anything already being held — before any other check,
        # so the filler/completeness checks below see the full combined
        # text rather than just this latest fragment. Shared with the
        # batch path so a fragment held by either can be completed by the
        # other.
        combined = await _combine_with_pending(transcript)

        # Filler-check, hold-vs-finalize decision (both smart hearing and
        # adaptive multi-part merging), and dispatch are all handled by the
        # shared pipeline below — identical to the batch STT path.
        await _finalize_or_hold(combined, language_detected)

    async def _on_stt_speech_start() -> None:
        # Recorded regardless of barge-in status — this is also the signal
        # used to measure how soon after the previous turn was dispatched
        # the caller started talking again (config.ENABLE_MULTI_PART_MERGE
        # "quick succession" detection in handle_user_turn below).
        turn_hold.mark_speech_started()
        # Sarvam's own VAD detected the caller starting to talk. If the
        # agent is mid-reply, this *is* the barge-in signal — replaces the
        # RMS-threshold sustained-energy check the batch path uses below,
        # per Sarvam's own documented barge-in guidance (stop playback,
        # close the TTS socket, start fresh next turn).
        if agent_speaking:
            await _trigger_barge_in("Sarvam VAD START_SPEECH")

    async def _on_stt_speech_end() -> None:
        # Informational — Sarvam finalizes and sends the transcript via the
        # "data" message (_on_stt_transcript above) independently, so no
        # action is required here. Kept as a named hook (rather than
        # silently ignoring the event) for future telemetry — e.g. logging
        # true end-of-speech-to-first-audio latency per turn.
        logger.debug(f"[{call_id}] Sarvam VAD END_SPEECH")

    async def _open_stt_session() -> Optional[StreamingSTTSession]:
        """(Re)open an ElevenLabs streaming STT session with the same callbacks
        used at call start. Returns None (and logs) if the connect attempt
        itself fails — the caller decides whether to retry or give up."""
        session = StreamingSTTSession(call_id, language_code=language_detected or "unknown")
        try:
            await session.connect(
                on_transcript=_on_stt_transcript,
                on_speech_start=_on_stt_speech_start,
                on_speech_end=_on_stt_speech_end,
                on_error=_on_stt_error,
            )
            return session
        except Exception as e:
            logger.warning(f"[{call_id}] Streaming STT (re)connect attempt failed: {e}")
            return None

    async def _speak_fallback_and_escalate() -> None:
        """
        Last resort when the ElevenLabs streaming STT session could not be
        recovered after config.STT_RECONNECT_MAX_ATTEMPTS tries: tell the
        caller (in speech, not silence) that there's a technical issue and
        the team will call back, then auto-escalate so a human actually
        follows up — instead of the call just going quietly dead from the
        caller's side, which is what happened before this fix.
        """
        nonlocal agent_speaking
        try:
            fallback_text = await config.get_stt_fallback_message()
            sentences = split_into_sentences(fallback_text)
            if sentences:
                agent_speaking = True
                if config.ENABLE_STREAMING_TTS:
                    await _speak_via_streaming_tts(
                        sentences, language_detected or "en-IN", lambda: False, "STT fallback"
                    )
                else:
                    for sentence in sentences:
                        audio = await tts_with_retry(sentence, language_detected or "en-IN")
                        if audio and stream_sid:
                            await _send_audio_to_twilio(websocket, audio, stream_sid)
                agent_speaking = False
        except Exception as e:
            logger.error(f"[{call_id}] Failed to speak STT fallback message: {e}")
            agent_speaking = False

        try:
            from brain.tools import execute_tool
            await execute_tool("escalate", {
                "reason": (
                    "Streaming STT session was lost mid-call and could not be "
                    "recovered after retrying — the caller could no longer be heard."
                ),
                "transcript_snippet": "",
                "call_id": call_id,
            })
            logger.info(f"[{call_id}] Auto-escalated after exhausting STT reconnect attempts")
        except Exception as e:
            logger.error(f"[{call_id}] Failed to auto-escalate after STT fallback: {e}")

    async def _on_stt_error(error: Exception) -> None:
        nonlocal stt_session, stt_reconnecting, stt_permanently_failed, stt_ever_connected
        logger.error(f"[{call_id}] Streaming STT session error: {error}")

        # Already permanently failed (fallback already spoken) or another
        # reconnect sequence is already in flight — nothing more to do.
        # Multiple send failures can report in quick succession right
        # after the socket dies; only one reconnect sequence should ever
        # run per outage.
        if stt_permanently_failed or stt_reconnecting:
            return

        stt_reconnecting = True
        try:
            for attempt in range(1, config.STT_RECONNECT_MAX_ATTEMPTS + 1):
                backoff_ms = min(
                    config.STT_RECONNECT_BACKOFF_BASE_MS * (2 ** (attempt - 1)),
                    config.STT_RECONNECT_BACKOFF_MAX_MS,
                )
                logger.info(
                    f"[{call_id}] Streaming STT reconnect attempt {attempt}/"
                    f"{config.STT_RECONNECT_MAX_ATTEMPTS} in {backoff_ms}ms"
                )
                await asyncio.sleep(backoff_ms / 1000)
                new_session = await _open_stt_session()
                if new_session is not None:
                    stt_session = new_session
                    stt_ever_connected = True
                    logger.info(f"[{call_id}] Streaming STT reconnected successfully on attempt {attempt}")
                    return

            logger.error(
                f"[{call_id}] Streaming STT reconnect exhausted after "
                f"{config.STT_RECONNECT_MAX_ATTEMPTS} attempts — falling back to "
                f"spoken apology + escalation"
            )
            stt_permanently_failed = True
            stt_session = None
            await _speak_fallback_and_escalate()
        finally:
            stt_reconnecting = False

    # ── WebSocket receive loop ────────────────────────────────────────────────

    try:
        async for raw_message in websocket.iter_text():
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            event = message.get("event")

            if event == "connected":
                logger.info(f"[{call_id}] Twilio stream connected")

            elif event == "start":
                start_data = message.get("start", {}) or {}
                stream_sid = start_data.get("streamSid")

                # REAL-BUG FIX: read the caller's number back out of the
                # customParameters Twilio echoes here — populated because
                # api/twilio_webhook.py now attaches <Parameter name="from">
                # on the <Stream> element. Falls back to "unknown" (the
                # previous, permanent behaviour) if it's ever missing, so
                # this is a strictly additive change with no new failure
                # mode.
                custom_params = start_data.get("customParameters", {}) or {}
                caller_phone_number = custom_params.get("from") or caller_phone_number
                call_direction = custom_params.get("direction") or call_direction

                logger.info(f"[{call_id}] Stream started: {stream_sid} | Caller: {mask_phone(caller_phone_number)}")
                if on_call_start:
                    asyncio.create_task(
                        on_call_start(call_id, caller_phone_number, call_direction)
                    )

                if config.ENABLE_STREAMING_STT:
                    stt_session = await _open_stt_session()
                    if stt_session is None:
                        logger.error(f"[{call_id}] Failed to open streaming STT session, "
                                     f"falling back to batch STT for this call")
                    else:
                        stt_ever_connected = True

                # Send greeting once — pipelined sentence-by-sentence
                if not greeting_sent:
                    greeting_sent  = True
                    greeting_text  = await config.get_greeting()
                    g_sentences    = split_into_sentences(greeting_text)
                    agent_speaking = True
                    agent_speech_start = time.time()

                    if config.ENABLE_STREAMING_TTS and g_sentences:
                        await _speak_via_streaming_tts(
                            g_sentences, "en-IN", lambda: False, "Greeting"
                        )

                    elif g_sentences:
                        g_prefetch = asyncio.create_task(
                            tts_with_retry(g_sentences[0], "en-IN")
                        )
                        for i, gs in enumerate(g_sentences):
                            if not agent_speaking:
                                if g_prefetch and not g_prefetch.done():
                                    g_prefetch.cancel()
                                break
                            try:
                                g_audio = await g_prefetch
                            except Exception:
                                g_audio = None
                            next_i = i + 1
                            if next_i < len(g_sentences):
                                g_prefetch = asyncio.create_task(
                                    tts_with_retry(g_sentences[next_i], "en-IN")
                                )
                            else:
                                g_prefetch = None
                            if g_audio and stream_sid:
                                await _send_audio_to_twilio(websocket, g_audio, stream_sid)
                    agent_speaking = False

            elif event == "media":
                payload = message.get("media", {}).get("payload", "")
                if not payload:
                    continue
                chunk     = base64.b64decode(payload)
                pcm_chunk = mulaw_to_pcm(chunk)

                if config.ENABLE_STREAMING_STT:
                    if stt_permanently_failed:
                        # Reconnect attempts exhausted earlier this call —
                        # caller has already heard the fallback message and
                        # the call has been escalated. Nothing useful to do
                        # with further audio.
                        continue
                    if stt_session is not None and not stt_session.closed:
                        # Sarvam's own VAD handles turn-taking and barge-in
                        # detection server-side (see _on_stt_speech_start /
                        # _on_stt_transcript above) — just forward every
                        # chunk continuously, agent-speaking or not,
                        # instead of the client-side buffer/RMS-threshold
                        # logic below.
                        # ElevenLabs streaming STT takes Twilio's native
                        # mulaw directly (audio_format=ulaw_8000, see
                        # voice/stt.py) — send the raw chunk, not our PCM
                        # conversion. One less conversion step on the path
                        # that actually reaches the STT provider.
                        await stt_session.send_audio(chunk)
                        continue
                    if stt_ever_connected:
                        # Was connected earlier this call, died, and a
                        # reconnect is currently in progress (_on_stt_error
                        # handles it in the background) — drop this chunk
                        # rather than silently switching this call over to
                        # the batch pipeline below, which was never
                        # initialized for it and expects different state.
                        continue
                    # Never connected even once this call (failed at call
                    # start, before _open_stt_session ever succeeded) —
                    # fall through to the batch STT path below on purpose,
                    # same as the original call-start fallback behaviour.

                # ── Batch STT path (existing behaviour) ─────────────────────

                # ── Barge-in detection while agent is speaking ─────────────
                if agent_speaking:
                    elapsed_since_start = time.time() - agent_speech_start

                    # Ignore audio during grace period
                    if elapsed_since_start < BARGE_IN_GRACE_PERIOD_SEC:
                        continue

                    rms = calculate_rms(pcm_chunk)
                    if rms >= BARGE_IN_RMS_THRESHOLD:
                        barge_in_speech_bytes += len(pcm_chunk)
                    else:
                        # Reset counter if energy drops — must be SUSTAINED
                        barge_in_speech_bytes = max(0, barge_in_speech_bytes - len(pcm_chunk) // 2)

                    if barge_in_speech_bytes >= BARGE_IN_THRESHOLD_BYTES:
                        await _trigger_barge_in("RMS threshold")
                        audio_buffer = pcm_chunk
                    continue

                # ── Accumulate caller audio ────────────────────────────────
                if not audio_buffer:
                    # Buffer was empty — this chunk is the start of a new
                    # utterance. Same signal as _on_stt_speech_start on the
                    # streaming path, used for the same adaptive multi-part
                    # "quick succession" gap check.
                    turn_hold.mark_speech_started()
                audio_buffer += pcm_chunk
                force_end     = len(audio_buffer) >= MAX_BUFFER_BYTES

                if len(audio_buffer) >= MIN_BUFFER_FOR_VAD and (
                    force_end or is_speech_ended(audio_buffer)
                ):
                    # Skip very short captures (< 100ms = likely noise)
                    if len(audio_buffer) < 1600:
                        audio_buffer = b""
                        continue

                    audio_snapshot = audio_buffer
                    audio_buffer   = b""
                    stt_task       = asyncio.create_task(
                        process_user_speech(audio_snapshot, language_detected)
                    )
                    pending_stt_tasks.append(stt_task)
                    stt_task.add_done_callback(
                        lambda t: pending_stt_tasks.remove(t)
                        if t in pending_stt_tasks else None
                    )

            elif event == "stop":
                logger.info(f"[{call_id}] Stream stopped")
                break

    except Exception as e:
        logger.error(f"[{call_id}] Stream error: {e}")
    finally:
        if current_turn_task and not current_turn_task.done():
            current_turn_task.cancel()
        for t in list(pending_stt_tasks):
            if not t.done():
                t.cancel()
        if pending_finalize_task and not pending_finalize_task.done():
            pending_finalize_task.cancel()

        if stt_session is not None:
            await stt_session.close()
        await _close_current_tts_session()

        duration  = int(time.time() - call_start_time)
        full_text = "\n".join(
            f"{t['role'].upper()}: {t['content']}" for t in turns
        )
        await clear_session(call_id)

        if on_call_end:
            asyncio.create_task(on_call_end(
                call_id       = call_id,
                duration      = duration,
                openai_cost   = total_openai_cost,
                sarvam_cost   = total_sarvam_cost,
                turns         = turns,
                full_text     = full_text,
            ))
        # VA-T-010 — surface discarded spend once per call so it is countable.
        # A rising share here means the barge-in / multi-part grace windows are
        # cutting turns off after the LLM has already been paid for.
        if discarded_turn_count:
            share = (
                (total_discarded_cost / total_openai_cost * 100)
                if total_openai_cost > 0
                else 0.0
            )
            logger.info(
                f"[{call_id}] Discarded turns: {discarded_turn_count} "
                f"(${total_discarded_cost:.5f}, {share:.1f}% of this call's LLM spend) "
                f"— answers generated and paid for but never spoken"
            )

        logger.info(f"[{call_id}] Call ended. Duration={duration}s")
        call_id_ctx.reset(call_id_ctx_token)