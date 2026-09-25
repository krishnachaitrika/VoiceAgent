import struct
import math
import logging
import config

logger = logging.getLogger(__name__)

# ── Thresholds tuned for Twilio mulaw → PCM phone audio ──────────────────────
#
# Phone line noise / static:  RMS ~150–400
# Quiet background room:      RMS ~300–600
# Normal speech on handset:   RMS ~800–4000
# Loud speech / shouting:     RMS ~4000–10000
#
# Sourced from config.py (VAD_SILENCE_RMS_THRESHOLD / VAD_SILENCE_THRESHOLD_MS)
# so these are tunable per-environment via .env, no code change/redeploy
# needed. See config.py for the full calibration notes on each value.
SILENCE_RMS_THRESHOLD = config.VAD_SILENCE_RMS_THRESHOLD

# ── Minimum buffer before VAD is even attempted ───────────────────────────────
# Below this size (250ms of audio) there is simply not enough data to make
# a reliable speech/silence decision. Checking smaller buffers wastes CPU
# and risks triggering on transient noise spikes (click, pop, network glitch).
# 8000 samples/sec * 2 bytes * 0.25s = 4000 bytes
MIN_BUFFER_FOR_VAD = 4000

# ── Lookback window for the has_speech() check inside is_speech_ended() ────
# is_speech_ended() only needs to know whether genuine sustained speech
# (has_speech's own 800ms / 8-consecutive-window requirement) occurred
# shortly before the trailing silence window it's about to check — not
# whether it occurred anywhere in the whole (unboundedly growing) buffer.
# 3000ms gives a generous safety margin over the bare 800ms minimum so
# that ordinary trailing noise (a cough, a click, a brief line crackle)
# between the real speech and the final pause doesn't push the speech
# evidence out of range. This keeps the has_speech scan a fixed, bounded
# size regardless of how large audio_buffer has grown.
SPEECH_LOOKBACK_MS = 3000


def calculate_rms(audio_bytes: bytes) -> float:
    """Calculate RMS energy of raw 16-bit PCM audio bytes."""
    if len(audio_bytes) < 2:
        return 0.0
    try:
        num_samples = len(audio_bytes) // 2
        samples = struct.unpack(f"<{num_samples}h", audio_bytes[:num_samples * 2])
        rms = math.sqrt(sum(s * s for s in samples) / num_samples)
        return rms
    except Exception as e:
        logger.warning(f"RMS calculation failed: {e}")
        return 0.0


def has_speech(audio_buffer: bytes, min_consecutive_windows: int = 8) -> bool:
    """
    Check whether the buffer contains genuine sustained speech.

    Two conditions must BOTH be true:
      1. At least 8 consecutive 100ms windows (800ms) of audio above
         SILENCE_RMS_THRESHOLD — rules out brief noise bursts (coughs,
         clicks, handling sounds, line crackle, echo bleed)
      2. At least 6 total windows (600ms) above threshold anywhere in the
         buffer — rules out scattered low-energy blips that add up by luck

    Enterprise reasoning:
      - A caller saying one syllable like "Hi" is ~200ms — below threshold.
        We wait for them to say at least a short sentence (800ms sustained).
      - Line echo from the agent's own TTS through the handset speaker is
        usually 100-200ms of above-threshold energy at most — filtered out.
      - Background TV / office noise is scattered, not sustained — filtered.
    """
    window_ms = 100
    bytes_per_ms = 16  # 8000 samples/sec * 2 bytes * (1/1000)
    window_size = window_ms * bytes_per_ms
    min_total_speech_windows = 6  # 600ms total above threshold

    if len(audio_buffer) < window_size:
        return False

    consecutive = 0
    total_speech_windows = 0
    found_sustained_run = False

    for i in range(0, len(audio_buffer) - window_size + 1, window_size):
        window = audio_buffer[i:i + window_size]
        if calculate_rms(window) >= SILENCE_RMS_THRESHOLD:
            consecutive += 1
            total_speech_windows += 1
            if consecutive >= min_consecutive_windows:
                found_sustained_run = True
        else:
            consecutive = 0

    return found_sustained_run and total_speech_windows >= min_total_speech_windows


def is_speech_ended(audio_buffer: bytes, silence_threshold_ms: int = None) -> bool:
    """
    Detect whether the caller has finished their utterance.

    silence_threshold_ms defaults to config.VAD_SILENCE_THRESHOLD_MS (see
    config.py for the full tradeoff notes and calibration history) — pass
    an explicit value here only if a specific call site genuinely needs a
    different window than the rest of the app.

    Two conditions must both hold:
      1. Buffer has genuine speech (has_speech passes) — prevents triggering
         on silence-only buffers at call start or during hold music
      2. The most recent `silence_threshold_ms` is silent — user has stopped
         speaking

    Only scans the tail of the buffer (not full rescan from byte 0).
    Full rescan every chunk = O(n^2) over call duration = CPU spike on
    long calls. Tail scan = O(1) regardless of buffer length.
    """
    if silence_threshold_ms is None:
        silence_threshold_ms = config.VAD_SILENCE_THRESHOLD_MS

    window_ms = 100
    bytes_per_ms = 16
    window_size = window_ms * bytes_per_ms

    # Require minimum buffer size before attempting VAD
    if len(audio_buffer) < MIN_BUFFER_FOR_VAD:
        return False

    silence_windows_needed = silence_threshold_ms // window_ms
    tail_size = silence_windows_needed * window_size

    # Bug fix: this used to call has_speech(audio_buffer) with the FULL
    # buffer, rescanning from byte 0 every single call — since this runs
    # once per ~20ms Twilio frame while audio_buffer grows toward its
    # MAX_BUFFER_BYTES cap, that made the whole utterance O(n^2). The only
    # thing has_speech needs to see is whether genuine sustained speech
    # (its own 8-consecutive-window / 800ms requirement) occurred
    # immediately before the trailing silence window we're about to check
    # below — so bound its input to that trailing silence window plus one
    # extra sustained-speech-sized window of lookback, instead of the
    # whole (unboundedly growing) buffer. This restores the O(1)-per-call,
    # tail-only behaviour this docstring already claimed.
    speech_lookback_size = (SPEECH_LOOKBACK_MS // window_ms) * window_size
    lookback_size = tail_size + speech_lookback_size
    lookback = audio_buffer[-lookback_size:] if len(audio_buffer) > lookback_size else audio_buffer

    if not has_speech(lookback):
        return False

    tail = audio_buffer[-tail_size:] if len(audio_buffer) > tail_size else audio_buffer

    if len(tail) < tail_size:
        return False

    consecutive_silent = 0
    for i in range(0, len(tail) - window_size + 1, window_size):
        window = tail[i:i + window_size]
        rms = calculate_rms(window)
        if rms < SILENCE_RMS_THRESHOLD:
            consecutive_silent += 1
            if consecutive_silent >= silence_windows_needed:
                return True
        else:
            consecutive_silent = 0
            break

    return consecutive_silent >= silence_windows_needed