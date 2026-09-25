import io
import re
import json
import time
import wave
import base64
import asyncio
import logging
from typing import Awaitable, Callable, Optional

import httpx
import websockets
import config

logger = logging.getLogger(__name__)

# ── STT provider: ElevenLabs Scribe v2 (batch) + Scribe v2 Realtime (streaming) ──
# STT was fully migrated off Sarvam here — see config.py's "Streaming +
# batch STT — ElevenLabs Scribe v2" section for the settings this module
# reads and the confidence notes on the exact wire protocol. Sarvam is not
# imported or referenced anywhere in this file any more. Sarvam TTS remains
# the default/fallback voice on the OUTPUT side only (voice/tts.py) —
# unrelated to this file.
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_STT_REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

# The audio buffer passed in here is raw 16-bit PCM, mono, 8kHz (converted
# from Twilio's mulaw stream via mulaw_to_pcm). ElevenLabs' batch endpoint
# expects a real audio file with a header, not bare PCM bytes — so we wrap
# it here, same as the old Sarvam path did.
PCM_SAMPLE_RATE = 8000
PCM_SAMPLE_WIDTH = 2  # bytes (16-bit) — used only by the batch WAV-wrapping path
MULAW_SAMPLE_WIDTH = 1  # byte/sample — used by the streaming path, which sends
# Twilio's native mulaw straight through (see ELEVENLABS_STT_AUDIO_FORMAT)
PCM_CHANNELS = 1

# ── Shared HTTP client ───────────────────────────────────────────────────────
# Pooled/reused client instead of opening a fresh connection per request —
# same reasoning as voice/tts.py's client (see that file for the fuller
# explanation of why this matters on a multi-turn call).
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(
                max_keepalive_connections=config.HTTP_POOL_KEEPALIVE_CONNECTIONS,
                max_connections=config.HTTP_POOL_MAX_CONNECTIONS,
            ),
        )
    return _client


async def close_stt_client() -> None:
    """Call this once on app shutdown (see main.py) to close the pooled
    connections cleanly instead of leaking them at process exit."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _pcm_to_wav_bytes(pcm_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(PCM_CHANNELS)
        wf.setsampwidth(PCM_SAMPLE_WIDTH)
        wf.setframerate(PCM_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _map_to_bcp47(elevenlabs_lang: Optional[str]) -> str:
    """English-only build: the agent no longer detects or switches
    language, so this always returns the one BCP-47 code the rest of the
    codebase (brain/ prompts, voice/tts.py, etc.) expects. Kept as a
    function (rather than inlining "en-IN" at every call site) so the
    signature doesn't need to change everywhere it's called."""
    return "en-IN"


# Real test-call finding: unlike Sarvam (which would occasionally hallucinate
# plausible-sounding *words* out of background noise), ElevenLabs Scribe
# tags non-speech audio with bracketed labels like "[static]", "[noise]",
# "[silence]", "[music]", "[laughter]", "[applause]", "[background noise]" —
# this is Scribe correctly telling us "this wasn't speech", which is a real
# accuracy improvement. But left unfiltered, that tag string was being
# treated as if the caller had literally said it (triggering a full
# orchestrator turn and a spoken reply to noise). This strips those tags
# before a transcript is treated as real caller speech, in both the batch
# and streaming paths below. Extend this list from real call logs if
# ElevenLabs emits other tags we haven't seen yet — do NOT try to guess the
# full tag vocabulary upfront.
_NON_SPEECH_TAG_RE = re.compile(
    r"\[(?:static|noise|silence|music|laughter|applause|background noise|inaudible|crosstalk)\]",
    re.IGNORECASE,
)


def _strip_non_speech_tags(text: str) -> str:
    """Remove ElevenLabs' bracketed non-speech tags and collapse any
    resulting extra whitespace. Returns "" if nothing but tags/whitespace
    was left — callers should treat an empty result as "no real speech",
    the same way they already treat a blank transcript."""
    if not text:
        return text
    cleaned = _NON_SPEECH_TAG_RE.sub("", text)
    return " ".join(cleaned.split())


async def _ws_connect_with_headers(url: str, headers: dict):
    """Open a websocket connection with a headers dict, tolerant of the
    `websockets` library version installed.

    v13+ renamed the connect() kwarg from `extra_headers` to
    `additional_headers`. requirements.txt pins websockets==13.1, but if a
    project's venv still has an older version installed (e.g. because
    `pip install -r requirements.txt` hasn't been re-run since this file was
    added), passing `additional_headers` raises:
        TypeError: ...create_connection() got an unexpected keyword
        argument 'additional_headers'
    which silently killed streaming STT and forced a fallback to slower
    batch STT. This tries the current kwarg name first, then falls back to
    the old one, so a stale `websockets` install degrades to a warning
    instead of breaking streaming entirely — fix the real cause by running
    `pip install -r requirements.txt` in the venv."""
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError as e:
        if "additional_headers" not in str(e):
            raise
        logger.warning(
            "websockets library is older than v13 (no 'additional_headers' support) — "
            "falling back to 'extra_headers'. Run `pip install -r requirements.txt` "
            "in this venv to get the pinned websockets==13.1 and remove this warning."
        )
        return await websockets.connect(url, extra_headers=headers)



async def transcribe_audio(audio_bytes: bytes, language_code: str = "unknown") -> tuple[str, str]:
    """
    Send audio bytes to ElevenLabs Scribe v2 (batch) for transcription.

    Returns:
        (transcript_text, detected_language_code)
    """
    if not audio_bytes:
        return "", "en-IN"

    start = time.perf_counter()
    try:
        wav_bytes = _pcm_to_wav_bytes(audio_bytes)
        files = {
            "file": ("audio.wav", io.BytesIO(wav_bytes), "audio/wav"),
        }
        data = {
            "model_id": config.ELEVENLABS_STT_MODEL,
            # English-only build: force the language rather than letting
            # Scribe auto-detect. This also removes the misdetection risk
            # that used to require the LANGUAGE_SWITCH_* hysteresis logic.
            "language_code": "en",
        }

        headers = {
            "xi-api-key": config.ELEVENLABS_API_KEY,
        }

        client = _get_client()
        response = await client.post(
            ELEVENLABS_STT_URL,
            headers=headers,
            files=files,
            data=data,
        )
        response.raise_for_status()
        result = response.json()

        elapsed = time.perf_counter() - start
        raw_transcript = result.get("text", "") or result.get("transcript", "")
        transcript = _strip_non_speech_tags(raw_transcript)
        detected_language = _map_to_bcp47(result.get("language_code"))
        if raw_transcript and not transcript:
            logger.info(
                f"STT complete (ElevenLabs Scribe v2): non-speech only ('{raw_transcript}') — "
                f"treating as no speech | latency={elapsed:.3f}s"
            )
        else:
            logger.info(
                f"STT complete (ElevenLabs Scribe v2): '{transcript[:50]}...' | "
                f"lang={detected_language} | latency={elapsed:.3f}s"
            )
        return transcript, detected_language

    except httpx.HTTPStatusError as e:
        logger.error(f"ElevenLabs STT HTTP error {e.response.status_code}: {e.response.text}")
        return "", "en-IN"
    except Exception as e:
        logger.error(f"ElevenLabs STT failed: {e}")
        return "", "en-IN"


# ── Streaming STT (v4 — ElevenLabs Scribe v2 Realtime WebSocket) ────────────
#
# StreamingSTTSession replaces "buffer the whole utterance client-side, wait
# for is_speech_ended() to see a fixed silence window, then fire one batch
# POST" with ElevenLabs' real-time transcription and server-side VAD
# (commit_strategy="vad" — see config.py). One session is opened per call
# and fed audio continuously, including while the agent is speaking, so
# barge-in can be detected — rather than one-shot per utterance.
#
# Public interface (connect/send_audio/flush/close/closed, and the
# on_transcript/on_speech_start/on_speech_end/on_error callback shapes) is
# UNCHANGED from the old Sarvam-backed version on purpose, so voice/stream.py
# needed zero changes for this migration.
class StreamingSTTSession:
    """
    One persistent ElevenLabs Scribe v2 Realtime WebSocket connection for
    the lifetime of a single call.

    Usage (see voice/stream.py for the real integration):

        session = StreamingSTTSession(call_id, language_code="unknown")
        await session.connect(
            on_transcript=my_on_transcript,       # async (text, lang, prob) -> None
            on_speech_start=my_on_speech_start,    # async () -> None
            on_speech_end=my_on_speech_end,        # async () -> None
        )
        ...
        await session.send_audio(pcm_chunk)   # call this for every inbound
                                               # Twilio media chunk, continuously
        ...
        await session.close()
    """

    def __init__(self, call_id: str, language_code: str = "unknown") -> None:
        self.call_id = call_id
        self.language_code = language_code
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False
        # Client-side batching buffer — see config.ELEVENLABS_STT_SEND_CHUNK_MS
        # docstring for why this exists (same lesson learned with Sarvam:
        # sending every raw ~20ms Twilio frame as its own message is far too
        # chatty for a real-time STT pipeline).
        #
        # Byte math uses MULAW_SAMPLE_WIDTH (1 byte/sample), not
        # PCM_SAMPLE_WIDTH (2 bytes/sample) — this session sends Twilio's
        # native mulaw straight through (audio_format=ulaw_8000), it does
        # NOT convert to PCM first (see send_audio() below and the
        # ELEVENLABS_STT_AUDIO_FORMAT config docstring for why). Using the
        # PCM byte width here was a real bug: it made this buffer flush at
        # roughly 2x the intended duration/2x too few chunks worth of
        # bytes, which does not match the audio_format actually declared to
        # ElevenLabs above.
        self._send_buffer = bytearray()
        self._send_chunk_bytes = int(
            PCM_SAMPLE_RATE * MULAW_SAMPLE_WIDTH * (config.ELEVENLABS_STT_SEND_CHUNK_MS / 1000)
        )
        self._chunks_sent = 0
        self._messages_received = 0
        self._speech_active = False  # tracks whether we've fired on_speech_start
        # for the current utterance yet, since Scribe v2 Realtime does not
        # emit an explicit START_SPEECH/END_SPEECH event pair the way
        # Sarvam did — see config.py's CONFIDENCE NOTE on this mapping.
        self._on_error: Optional[Callable[[Exception], Awaitable[None]]] = None

    async def connect(
        self,
        on_transcript: Optional[Callable[[str, Optional[str], Optional[float]], Awaitable[None]]] = None,
        on_speech_start: Optional[Callable[[], Awaitable[None]]] = None,
        on_speech_end: Optional[Callable[[], Awaitable[None]]] = None,
        on_error: Optional[Callable[[Exception], Awaitable[None]]] = None,
    ) -> None:
        self._on_error = on_error

        params = {
            "model_id": config.ELEVENLABS_STT_REALTIME_MODEL,
            "audio_format": config.ELEVENLABS_STT_AUDIO_FORMAT,
            "commit_strategy": config.ELEVENLABS_STT_COMMIT_STRATEGY,
            # English-only build: force the language rather than letting
            # Scribe auto-detect across its full language set — removes the
            # misdetection risk that used to require secondary_languages
            # narrowing plus the LANGUAGE_SWITCH_* hysteresis logic.
            "language_code": "en",
        }
        if config.ELEVENLABS_STT_VAD_THRESHOLD:
            params["vad_threshold"] = config.ELEVENLABS_STT_VAD_THRESHOLD
        if config.ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS:
            params["vad_silence_threshold_secs"] = config.ELEVENLABS_STT_VAD_SILENCE_THRESHOLD_SECS
        if config.ELEVENLABS_STT_MIN_SPEECH_DURATION_MS:
            params["min_speech_duration_ms"] = config.ELEVENLABS_STT_MIN_SPEECH_DURATION_MS
        if config.ELEVENLABS_STT_MIN_SILENCE_DURATION_MS:
            params["min_silence_duration_ms"] = config.ELEVENLABS_STT_MIN_SILENCE_DURATION_MS
        query_parts = [f"{k}={v}" for k, v in params.items()]
        query = "&".join(query_parts)
        url = f"{ELEVENLABS_STT_REALTIME_URL}?{query}"

        self._ws = await _ws_connect_with_headers(url, {"xi-api-key": config.ELEVENLABS_API_KEY})
        self._recv_task = asyncio.create_task(
            self._receive_loop(on_transcript, on_speech_start, on_speech_end, on_error)
        )
        logger.info(
            f"[{self.call_id}] ElevenLabs streaming STT connected "
            f"(model={config.ELEVENLABS_STT_REALTIME_MODEL})"
        )

    async def _receive_loop(self, on_transcript, on_speech_start, on_speech_end, on_error) -> None:
        first_message_seen = False
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning(f"[{self.call_id}] ElevenLabs streaming STT: non-JSON message dropped")
                    continue

                msg_type = msg.get("message_type")
                self._messages_received += 1
                if not first_message_seen:
                    first_message_seen = True
                    logger.info(f"[{self.call_id}] ElevenLabs streaming STT: first message received (type={msg_type})")

                if msg_type == "session_started":
                    logger.info(f"[{self.call_id}] ElevenLabs streaming STT: session_started ({msg.get('session_id')})")

                elif msg_type == "partial_transcript":
                    # First partial of a fresh utterance is the closest signal
                    # we have to Sarvam's START_SPEECH — see the confidence
                    # note above and in config.py.
                    if not self._speech_active:
                        self._speech_active = True
                        if on_speech_start:
                            await on_speech_start()
                    # Partial transcripts are not forwarded as final text —
                    # only committed/final transcripts are (same "don't fire
                    # RAG/GPT on a half sentence" principle as smart hearing
                    # already applies downstream in voice/stream.py).

                elif msg_type in ("final_transcript", "committed_transcript"):
                    raw_transcript = msg.get("text", "") or ""
                    transcript = _strip_non_speech_tags(raw_transcript)
                    detected_lang = msg.get("language_code")
                    lang_probability = msg.get("language_probability")
                    if raw_transcript and not transcript:
                        logger.info(
                            f"[{self.call_id}] ElevenLabs streaming STT: {msg_type} was non-speech only "
                            f"('{raw_transcript}') — not forwarding as caller speech"
                        )
                    else:
                        logger.info(
                            f"[{self.call_id}] ElevenLabs streaming STT: {msg_type} "
                            f"'{transcript[:60]}' (lang={detected_lang}, confidence={lang_probability})"
                        )
                    if self._speech_active:
                        self._speech_active = False
                        if on_speech_end:
                            await on_speech_end()
                    if transcript.strip() and on_transcript:
                        # English-only build: language is always "en-IN" —
                        # see _map_to_bcp47 above.
                        await on_transcript(transcript, "en-IN", lang_probability)

                elif msg_type == "rate_limited":
                    logger.warning(f"[{self.call_id}] ElevenLabs streaming STT rate limited: {msg.get('message', msg)}")

                elif "error" in msg:
                    logger.warning(f"[{self.call_id}] ElevenLabs streaming STT error event: {msg}")

                else:
                    logger.warning(
                        f"[{self.call_id}] ElevenLabs streaming STT: unrecognized message "
                        f"type={msg_type!r} data={msg}"
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"[{self.call_id}] ElevenLabs streaming STT receive loop ended: {e}")
            self._closed = True
            if on_error:
                await on_error(e)

    @property
    def closed(self) -> bool:
        """True once this session's socket is known dead (either side) —
        voice/stream.py checks this to decide whether a reconnect is
        needed before continuing to forward audio."""
        return self._closed

    async def send_audio(self, mulaw_bytes: bytes) -> None:
        """Buffer one chunk of raw Twilio mulaw audio (8kHz, mono — the
        exact bytes Twilio sends, no conversion needed since this session
        connects with audio_format=ulaw_8000, see
        config.ELEVENLABS_STT_AUDIO_FORMAT) — Twilio delivers these in
        ~20ms frames, but each is only appended to an internal buffer here
        and actually sent to ElevenLabs once
        config.ELEVENLABS_STT_SEND_CHUNK_MS worth has accumulated.

        Call this continuously as Twilio media chunks arrive — no
        additional buffering or PCM conversion needed by the caller."""
        if self._closed or not self._ws or not mulaw_bytes:
            return
        self._send_buffer.extend(mulaw_bytes)
        if len(self._send_buffer) >= self._send_chunk_bytes:
            await self._flush_send_buffer()

    async def _flush_send_buffer(self, commit: bool = False) -> None:
        if not self._send_buffer and not commit:
            return
        audio_to_send = bytes(self._send_buffer)
        self._send_buffer.clear()
        await self._send_audio_chunk(audio_to_send, commit=commit)

    async def _send_audio_chunk(self, mulaw_bytes: bytes, commit: bool = False) -> None:
        """Base64-encode raw mulaw bytes and send as an input_audio_chunk
        message — ElevenLabs' realtime endpoint takes bare audio bytes
        directly (no WAV wrapper needed per-chunk, unlike the batch
        endpoint above), in whatever format was declared via audio_format
        at connect time (config.ELEVENLABS_STT_AUDIO_FORMAT)."""
        if self._closed or not self._ws:
            return
        if not mulaw_bytes and not commit:
            return
        try:
            payload = {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(mulaw_bytes).decode("utf-8"),
            }
            if commit:
                payload["commit"] = True
            await self._ws.send(json.dumps(payload))
            self._chunks_sent += 1
            if self._chunks_sent % 25 == 0:
                logger.info(
                    f"[{self.call_id}] ElevenLabs streaming STT activity: "
                    f"{self._chunks_sent} chunk(s) sent, "
                    f"{self._messages_received} message(s) received back"
                )
        except websockets.exceptions.ConnectionClosed as e:
            if not self._closed:
                self._closed = True
                logger.error(
                    f"[{self.call_id}] ElevenLabs streaming STT socket closed unexpectedly "
                    f"({e}) — no more audio will be sent on this session for this call."
                )
                if self._on_error:
                    await self._on_error(e)
        except Exception as e:
            logger.warning(f"[{self.call_id}] Failed to send audio to ElevenLabs streaming STT: {e}")

    async def flush(self) -> None:
        """Force-finalize whatever ElevenLabs currently has buffered, by
        sending any partial client-side chunk with commit=True — mirrors the
        old Sarvam ws.flush() manual safety cap (e.g. as a MAX_BUFFER_BYTES-
        equivalent), rather than relying purely on VAD end-of-speech."""
        if self._closed or not self._ws:
            return
        await self._flush_send_buffer(commit=True)

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._flush_send_buffer()
        except Exception:
            pass
        self._closed = True
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.warning(f"[{self.call_id}] Error closing ElevenLabs streaming STT: {e}")
            finally:
                self._ws = None