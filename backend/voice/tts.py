import base64
import json
import time
import asyncio
import logging
from typing import Optional

import httpx
import websockets
from sarvamai import AsyncSarvamAI
import config
from voice.stt import _ws_connect_with_headers
from cache.settings_cache import get_live_settings

logger = logging.getLogger(__name__)

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"

# ── Shared HTTP client ───────────────────────────────────────────────────────
# Previously each _sarvam_tts()/_elevenlabs_tts() call opened its own
# `async with httpx.AsyncClient(...)` — meaning every single TTS call (and a
# turn can fire 3-5 of these back to back, per the sentence-pipelining in
# voice/stream.py) paid a fresh TCP connect + TLS handshake to Sarvam or
# ElevenLabs, then threw the connection away immediately after.
#
# One shared, persistent client with keep-alive lets httpx reuse the same
# underlying connection across calls to the same host, skipping that
# handshake on every call after the first. This is a purely mechanical
# change — the request/response logic, retries, and fallback order below
# are untouched, so behaviour on success/failure/timeout is identical to
# before, just without the repeated connection setup cost.
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


async def close_tts_client() -> None:
    """Call this once on app shutdown (see main.py) to close the pooled
    connections cleanly instead of leaking them at process exit."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def synthesize_speech(
    text: str,
    language_code: str = "en-IN",
    sample_rate: Optional[int] = None,
) -> bytes:
    """
    Synthesize speech from text.

    Provider selection previously ignored the Settings dashboard's "Voice
    Provider" dropdown entirely — it only checked whether an ElevenLabs
    key+voice ID happened to exist in .env, so changing the dropdown had
    zero effect on the running agent. Now reads the live "voice_provider"
    setting (see cache/settings_cache.py) — ElevenLabs is only used when
    BOTH the dashboard is set to "elevenlabs" AND a real API key exists in
    .env (the API key itself stays a secret in .env, never stored in the
    DB — only the choice of provider and the voice ID live in Settings).

    Args:
        sample_rate: Sarvam output rate. Defaults to config.SARVAM_TTS_OUTPUT_SAMPLE_RATE
            (8kHz, the phone line's rate). The browser mock interview passes a
            higher rate because it plays audio over speakers, not a phone line.
            ElevenLabs ignores it.

    Returns:
        Raw audio bytes — WAV from Sarvam, MP3 from ElevenLabs

    Note (VA-C2): this batch path (used only when config.ENABLE_STREAMING_TTS
    is false — not the default) is not cost-metered. StreamingTTSSession's
    cost_usd() covers the default streaming path; this one would need the
    same per-provider character counting added here if it's ever the
    primary path in a deployment.
    """
    if not text.strip():
        return b""

    live = await get_live_settings()
    wants_elevenlabs = live.get("voice_provider") == "elevenlabs"
    voice_id = live.get("elevenlabs_voice_id") or config.ELEVENLABS_VOICE_ID

    if wants_elevenlabs and config.ELEVENLABS_API_KEY and voice_id:
        audio = await _elevenlabs_tts(text, voice_id)
        if audio:
            return audio
        logger.warning("ElevenLabs selected in Settings but the call failed — falling back to Sarvam")

    # Default / fallback: Sarvam Bulbul v3
    return await _sarvam_tts(text, language_code, sample_rate=sample_rate)


async def _elevenlabs_tts(text: str, voice_id: str) -> bytes:
    """Call ElevenLabs TTS (batch) — model_id from config.ELEVENLABS_TTS_MODEL."""
    start = time.perf_counter()
    try:
        url = f"{ELEVENLABS_TTS_URL}/{voice_id}"
        headers = {
            "xi-api-key": config.ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": config.ELEVENLABS_TTS_MODEL,
            "voice_settings": {
                "stability": config.ELEVENLABS_TTS_STABILITY,
                "similarity_boost": config.ELEVENLABS_TTS_SIMILARITY_BOOST,
            },
        }
        client = _get_client()
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        audio_bytes = response.content

        elapsed = time.perf_counter() - start
        logger.info(f"ElevenLabs TTS: {len(audio_bytes)} bytes in {elapsed:.3f}s")
        return audio_bytes

    except Exception as e:
        logger.error(f"ElevenLabs TTS failed, falling back to Bulbul: {e}")
        return b""


async def _sarvam_tts(text: str, language_code: str = "en-IN", sample_rate: Optional[int] = None) -> bytes:
    """Call Sarvam Bulbul v3 for TTS."""
    start = time.perf_counter()
    try:
        headers = {
            "api-subscription-key": config.SARVAM_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": [text],
            "target_language_code": "en-IN",
            "speaker": config.SARVAM_TTS_SPEAKER,
            "model": config.SARVAM_TTS_MODEL,
            "enable_preprocessing": True,
            "speech_sample_rate": sample_rate or config.SARVAM_TTS_OUTPUT_SAMPLE_RATE,
        }
        client = _get_client()
        response = await client.post(SARVAM_TTS_URL, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

        audios = result.get("audios", [])
        if not audios:
            logger.warning("Sarvam TTS returned no audio")
            return b""

        audio_bytes = base64.b64decode(audios[0])
        elapsed = time.perf_counter() - start
        logger.info(f"Sarvam Bulbul TTS: {len(audio_bytes)} bytes in {elapsed:.3f}s")
        return audio_bytes

    except Exception as e:
        logger.error(f"Sarvam TTS failed: {e}")
        return b""

# ── Streaming TTS (v4 — Sarvam WebSocket, see ENABLE_STREAMING_TTS) ─────────
#
# StreamingTTSSession replaces "wait for a full sentence of text, POST it,
# wait for the whole audio clip back" (the _sarvam_tts call above) with
# Sarvam's WebSocket TTS: text is streamed in as it becomes available (e.g.
# token-by-token from a streaming GPT call, or sentence-by-sentence from
# voice/stream.py's existing split_into_sentences pipeline), and audio
# chunks stream back progressively — playback can start on the first chunk
# instead of waiting for Sarvam to finish synthesizing the whole thing.
#
# All tunables (speaker, pace, buffer sizes, output codec/sample rate) come
# from config.py — no literal values live in this file.
#
# CONFIDENCE NOTE: the message shapes below (`ws.configure(...)`,
# `ws.convert(text)`, `ws.flush()`, and the `AudioOutput` / `EventResponse`
# response types with `output_audio_codec` accepting "mulaw") are taken
# directly from the installed `sarvamai` SDK's own source
# (text_to_speech_streaming/socket_client.py, types/audio_output.py,
# types/configure_connection_data_output_audio_codec.py) — high confidence,
# but re-check those files after any `sarvamai` version bump.

_sarvam_client: Optional[AsyncSarvamAI] = None


def _get_sarvam_streaming_client() -> AsyncSarvamAI:
    global _sarvam_client
    if _sarvam_client is None:
        _sarvam_client = AsyncSarvamAI(api_subscription_key=config.SARVAM_API_KEY)
    return _sarvam_client


class _SarvamStreamingTTSSession:
    """
    One Sarvam text-to-speech WebSocket connection, meant to live for one
    agent turn (open it, stream in the sentences of that turn's reply as
    they're ready, close it — barge-in also closes it early, see
    voice/stream.py's barge-in handling and the Sarvam TTS docs' note that
    there is no in-band cancel, only "stop playback locally + close the
    socket + open a fresh one for the next turn").

    This is the default/fallback streaming TTS implementation — selected by
    the public StreamingTTSSession wrapper below whenever the Settings
    dashboard's Voice Provider isn't set to ElevenLabs (or no voice ID is
    configured). Usage:
        session = _SarvamStreamingTTSSession(call_id, language_code="en-IN")
        await session.connect()
        await session.send_text("First sentence of the reply.")
        await session.send_text("Second sentence, as soon as it's ready.")
        await session.finish()  # flushes any remainder, signals no more text
        async for audio_bytes, content_type in session.audio_chunks():
            ...forward audio_bytes to Twilio as it arrives...
        await session.close()
    """

    def __init__(self, call_id: str, language_code: str = "en-IN") -> None:
        self.call_id = call_id
        self.language_code = language_code
        self._ws_cm = None
        self._ws = None
        self._configured = False
        self._finished = False

    async def connect(self) -> None:
        client = _get_sarvam_streaming_client()
        self._ws_cm = client.text_to_speech_streaming.connect(
            model=config.SARVAM_TTS_MODEL,
            send_completion_event="true",
            api_subscription_key=config.SARVAM_API_KEY,
        )
        self._ws = await self._ws_cm.__aenter__()
        logger.info(f"[{self.call_id}] Sarvam streaming TTS connected (model={config.SARVAM_TTS_MODEL})")

    async def _ensure_configured(self) -> None:
        if self._configured:
            return
        # Same live-Settings-dashboard lookup the batch _sarvam_tts() call
        # uses below, so a Settings change (once a "sarvam_speaker" key
        # exists there) takes effect on the streaming path too, without a
        # code change — falls back to config.SARVAM_TTS_SPEAKER until then.
        live = await get_live_settings()
        speaker = live.get("sarvam_speaker") or config.SARVAM_TTS_SPEAKER

        logger.info(
            f"[{self.call_id}] Sarvam streaming TTS: sending configure "
            f"(speaker={speaker}, lang=en-IN, "
            f"codec={config.SARVAM_TTS_OUTPUT_CODEC}, "
            f"sample_rate={config.SARVAM_TTS_OUTPUT_SAMPLE_RATE})"
        )
        await self._ws.configure(
            target_language_code="en-IN",
            speaker=speaker,
            pace=config.SARVAM_TTS_PACE,
            min_buffer_size=config.SARVAM_TTS_MIN_BUFFER_SIZE,
            max_chunk_length=config.SARVAM_TTS_MAX_CHUNK_LENGTH,
            output_audio_codec=config.SARVAM_TTS_OUTPUT_CODEC,
            speech_sample_rate=config.SARVAM_TTS_OUTPUT_SAMPLE_RATE,
            enable_preprocessing=True,
        )
        self._configured = True
        logger.info(f"[{self.call_id}] Sarvam streaming TTS: configure sent OK")

    async def send_text(self, text: str) -> None:
        """Stream one chunk of reply text (a sentence, a partial GPT token
        buffer — whatever unit the caller has ready) into the session."""
        if not text or not text.strip() or not self._ws:
            return
        await self._ensure_configured()
        logger.info(f"[{self.call_id}] Sarvam streaming TTS: sending text chunk ({len(text)} chars)")
        await self._ws.convert(text)
        logger.info(f"[{self.call_id}] Sarvam streaming TTS: text chunk sent OK")



    async def finish(self) -> None:
        """Call once there is no more text coming for this turn — flushes
        anything still sitting below min_buffer_size so it actually gets
        synthesized instead of waiting forever for more text that never
        arrives."""
        if not self._ws or self._finished:
            return
        self._finished = True
        try:
            await self._ws.flush()
        except Exception as e:
            logger.warning(f"[{self.call_id}] Failed to flush Sarvam streaming TTS: {e}")

    async def audio_chunks(self):
        """
        Async generator yielding (audio_bytes, content_type) tuples as
        Sarvam synthesizes them — the caller should forward each chunk to
        Twilio as it arrives rather than collecting them all first, which
        is the entire latency point of the streaming path.

        Ends on whichever comes first: Sarvam's "final" completion event
        (send_completion_event=true, set in connect() above), the socket
        closing, or config.SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC of silence
        with no new message — real testing showed the socket can go quiet
        after the last real audio chunk without ever sending a final event
        or closing, so this idle timeout is what actually ends the turn in
        that case instead of hanging until the much longer
        STREAMING_TTS_TURN_TIMEOUT_SEC call-level backstop.
        """
        if not self._ws:
            return
        logger.info(f"[{self.call_id}] Sarvam streaming TTS: waiting for audio...")
        first_message_seen = False
        chunk_count = 0
        message_iter = self._ws.__aiter__()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        message_iter.__anext__(),
                        timeout=config.SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[{self.call_id}] Sarvam streaming TTS: no message for "
                        f"{config.SARVAM_TTS_AUDIO_IDLE_TIMEOUT_SEC}s after {chunk_count} "
                        f"audio chunk(s) — treating turn as complete"
                    )
                    return
                except StopAsyncIteration:
                    logger.info(f"[{self.call_id}] Sarvam streaming TTS: socket closed after {chunk_count} chunk(s)")
                    return

                msg_type = getattr(message, "type", None)
                if not first_message_seen:
                    first_message_seen = True
                    logger.info(f"[{self.call_id}] Sarvam streaming TTS: first message received (type={msg_type})")

                if msg_type == "audio":
                    chunk_count += 1
                    audio_bytes = base64.b64decode(message.data.audio)
                    yield audio_bytes, getattr(message.data, "content_type", "")
                elif msg_type == "event":
                    if getattr(message.data, "event_type", None) == "final":
                        logger.info(f"[{self.call_id}] Sarvam streaming TTS: final event after {chunk_count} chunk(s)")
                        return
                elif msg_type == "error":
                    logger.warning(f"[{self.call_id}] Sarvam streaming TTS error: {message}")
                    return
                else:
                    logger.warning(f"[{self.call_id}] Sarvam streaming TTS: unrecognized message type={msg_type!r} data={message}")
        except Exception as e:
            logger.info(f"[{self.call_id}] Sarvam streaming TTS audio stream ended: {e}")

    async def close(self) -> None:
        """Close this turn's TTS socket. Per Sarvam's own barge-in guidance,
        there is no in-band cancel — on barge-in, stop local playback
        first, then call this, then open a fresh StreamingTTSSession for
        the next turn (voice/stream.py does exactly this)."""
        if self._ws_cm is not None:
            try:
                await self._ws_cm.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[{self.call_id}] Error closing Sarvam streaming TTS: {e}")
            finally:
                self._ws_cm = None
                self._ws = None


# ── ElevenLabs streaming TTS ─────────────────────────────────────────────────
# Selected by the public StreamingTTSSession wrapper below only when the
# Settings dashboard's Voice Provider is "elevenlabs" AND a real
# elevenlabs_voice_id is configured (same condition the batch
# synthesize_speech() path already uses) — this is the streaming
# counterpart to that existing switch, not a separate/new toggle.
#
# See config.py's CONFIDENCE NOTE (ElevenLabs TTS section) for the exact
# wire-protocol assumptions this implementation makes; re-verify against the
# live ElevenLabs WebSocket TTS API reference after any version bump.
class _ElevenLabsStreamingTTSSession:
    """
    One ElevenLabs text-to-speech WebSocket connection
    (/v1/text-to-speech/{voice_id}/stream-input), meant to live for one
    agent turn — same lifecycle contract as _SarvamStreamingTTSSession
    above (connect → send_text(...) → finish() → audio_chunks() → close()),
    so the public StreamingTTSSession wrapper can use either implementation
    interchangeably without voice/stream.py knowing which one is active.
    """

    def __init__(self, call_id: str, voice_id: str, language_code: str = "en-IN") -> None:
        self.call_id = call_id
        self.voice_id = voice_id
        self.language_code = language_code
        self._ws = None
        self._configured = False
        self._finished = False

    async def connect(self) -> None:
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input"
            f"?model_id={config.ELEVENLABS_TTS_STREAMING_MODEL}"
            f"&output_format={config.ELEVENLABS_TTS_OUTPUT_FORMAT}"
        )
        self._ws = await _ws_connect_with_headers(url, {"xi-api-key": config.ELEVENLABS_API_KEY})
        logger.info(
            f"[{self.call_id}] ElevenLabs streaming TTS connected "
            f"(model={config.ELEVENLABS_TTS_STREAMING_MODEL}, voice={self.voice_id})"
        )

    async def _ensure_configured(self) -> None:
        if self._configured:
            return
        await self._ws.send(json.dumps({
            "text": " ",
            "voice_settings": {
                "stability": config.ELEVENLABS_TTS_STABILITY,
                "similarity_boost": config.ELEVENLABS_TTS_SIMILARITY_BOOST,
            },
            "xi_api_key": config.ELEVENLABS_API_KEY,
        }))
        self._configured = True
        logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: configure sent OK")

    async def send_text(self, text: str) -> None:
        """Stream one chunk of reply text into the session."""
        if not text or not text.strip() or not self._ws:
            return
        await self._ensure_configured()
        logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: sending text chunk ({len(text)} chars)")
        await self._ws.send(json.dumps({"text": text}))
        logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: text chunk sent OK")

    async def finish(self) -> None:
        """Call once there is no more text coming for this turn — sends the
        empty-string message ElevenLabs uses to signal end-of-input and
        flush whatever's still buffered."""
        if not self._ws or self._finished:
            return
        self._finished = True
        try:
            await self._ensure_configured()
            await self._ws.send(json.dumps({"text": ""}))
        except Exception as e:
            logger.warning(f"[{self.call_id}] Failed to finish ElevenLabs streaming TTS: {e}")

    async def audio_chunks(self):
        """
        Async generator yielding (audio_bytes, content_type) tuples as
        ElevenLabs synthesizes them. Ends on whichever comes first:
        ElevenLabs' isFinal=true message, the socket closing, or
        config.ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC of silence with no new
        message — same defensive idle-timeout pattern as the Sarvam
        implementation above, in case the socket goes quiet without a clean
        final signal.
        """
        if not self._ws:
            return
        logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: waiting for audio...")
        chunk_count = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        self._ws.recv(),
                        timeout=config.ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[{self.call_id}] ElevenLabs streaming TTS: no message for "
                        f"{config.ELEVENLABS_TTS_AUDIO_IDLE_TIMEOUT_SEC}s after {chunk_count} "
                        f"audio chunk(s) — treating turn as complete"
                    )
                    return
                except websockets.exceptions.ConnectionClosed:
                    logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: socket closed after {chunk_count} chunk(s)")
                    return

                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                if message.get("audio"):
                    chunk_count += 1
                    audio_bytes = base64.b64decode(message["audio"])
                    content_type = f"audio/{config.ELEVENLABS_TTS_OUTPUT_FORMAT}"
                    yield audio_bytes, content_type

                if message.get("isFinal"):
                    logger.info(f"[{self.call_id}] ElevenLabs streaming TTS: final flag after {chunk_count} chunk(s)")
                    return
        except Exception as e:
            logger.info(f"[{self.call_id}] ElevenLabs streaming TTS audio stream ended: {e}")

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.warning(f"[{self.call_id}] Error closing ElevenLabs streaming TTS: {e}")
            finally:
                self._ws = None


class StreamingTTSSession:
    """
    Public streaming-TTS entry point used by voice/stream.py — UNCHANGED
    constructor/method signature from before this migration
    (connect/send_text/finish/audio_chunks/close), so voice/stream.py needed
    zero changes.

    Internally picks a provider per the exact same rule the batch
    synthesize_speech() path already uses: ElevenLabs only when BOTH the
    live Settings "voice_provider" is "elevenlabs" AND a real voice ID is
    configured; Sarvam Bulbul otherwise. If ElevenLabs is selected but the
    connection attempt itself fails, this falls back to Sarvam for the rest
    of the turn rather than leaving the caller with dead air — same
    fail-open philosophy as the batch path's fallback.
    """

    def __init__(self, call_id: str, language_code: str = "en-IN") -> None:
        self.call_id = call_id
        self.language_code = language_code
        self._impl = None  # set on connect()
        self._provider: Optional[str] = None  # "elevenlabs" | "sarvam" — set on connect()
        # Characters actually sent for synthesis this session (VA-C2 fix) —
        # the only per-session number needed to meter TTS cost; see
        # cost_usd() below. Counting here (this class's one send choke
        # point) rather than in voice/stream.py keeps the metering next to
        # the provider selection that decides which rate applies.
        self.characters_sent: int = 0

    async def connect(self) -> None:
        live = await get_live_settings()
        wants_elevenlabs = live.get("voice_provider") == "elevenlabs"
        voice_id = live.get("elevenlabs_voice_id") or config.ELEVENLABS_VOICE_ID

        if wants_elevenlabs and config.ELEVENLABS_API_KEY and voice_id:
            candidate = _ElevenLabsStreamingTTSSession(self.call_id, voice_id, self.language_code)
            try:
                await candidate.connect()
                self._impl = candidate
                self._provider = "elevenlabs"
                return
            except Exception as e:
                logger.warning(
                    f"[{self.call_id}] ElevenLabs streaming TTS selected in Settings but connect "
                    f"failed ({e}) — falling back to Sarvam streaming TTS for this turn"
                )

        self._impl = _SarvamStreamingTTSSession(self.call_id, self.language_code)
        self._provider = "sarvam"
        await self._impl.connect()

    async def send_text(self, text: str) -> None:
        if self._impl is not None:
            self.characters_sent += len(text)
            await self._impl.send_text(text)

    def cost_usd(self) -> float:
        """USD cost of everything sent through this session so far, at
        config's per-1K-character rate for whichever provider actually
        handled it. 0.0 if connect() never picked a provider (nothing was
        sent) or the configured rate is still 0.0 (VA-C2 fix — see
        config.py's ELEVENLABS_TTS_COST_PER_1K_CHARS docstring)."""
        rate = {
            "elevenlabs": config.ELEVENLABS_TTS_COST_PER_1K_CHARS,
            "sarvam": config.SARVAM_TTS_COST_PER_1K_CHARS,
        }.get(self._provider, 0.0)
        return (self.characters_sent / 1000) * rate

    async def finish(self) -> None:
        if self._impl is not None:
            await self._impl.finish()

    async def audio_chunks(self):
        if self._impl is None:
            return
        async for chunk in self._impl.audio_chunks():
            yield chunk

    async def close(self) -> None:
        if self._impl is not None:
            await self._impl.close()