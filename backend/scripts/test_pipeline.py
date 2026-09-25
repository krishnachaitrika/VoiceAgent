"""
Test the full pipeline without a telephony provider:
  cd backend
  python scripts/test_pipeline.py

Reads audio from microphone (5 seconds), sends through STT -> Agent -> TTS -> plays back.
"""
import asyncio
import sys
import os
import time
import wave
import io
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from voice.stt import transcribe_audio, close_stt_client
from voice.tts import synthesize_speech, close_tts_client
from cache.settings_cache import get_live_settings
from brain.agent import process_turn
import config


async def test_pipeline():
    print("=" * 60)
    print(f"  {config.COMPANY_NAME} Voice Agent — Pipeline Test")
    print("=" * 60)

    # Check ElevenLabs config upfront so a silent Sarvam fallback doesn't
    # look like a bug. TTS provider is decided by cache/settings_cache.py:
    # it uses ElevenLabs only if the live "voice_provider" setting is
    # "elevenlabs" AND both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are
    # set in .env — missing either one falls back to Sarvam with no error.
    elevenlabs_ready = bool(config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID)
    if not elevenlabs_ready:
        missing = []
        if not config.ELEVENLABS_API_KEY:
            missing.append("ELEVENLABS_API_KEY")
        if not config.ELEVENLABS_VOICE_ID:
            missing.append("ELEVENLABS_VOICE_ID")
        print(f"\n⚠️  {', '.join(missing)} not set in .env — TTS will use Sarvam, not ElevenLabs.")

    live = await get_live_settings()
    tts_provider = "elevenlabs" if (live.get("voice_provider") == "elevenlabs" and elevenlabs_ready) else "sarvam"

    # Try to import sounddevice for mic recording
    audio_bytes = None
    try:
        import sounddevice as sd
        import numpy as np

        sample_rate = 16000
        duration = 5
        print(f"\n🎤 Recording {duration} seconds of audio... (speak now!)")
        recording = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        )
        sd.wait()
        print("  ✅ Recording complete")

        # Convert to WAV bytes
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(recording.tobytes())
        audio_bytes = wav_buffer.getvalue()

    except ImportError:
        print("\n⚠️  sounddevice not available. Using a test audio file instead.")
        test_audio_path = os.path.join(os.path.dirname(__file__), "test_audio.wav")
        if os.path.exists(test_audio_path):
            with open(test_audio_path, "rb") as f:
                audio_bytes = f.read()
        else:
            print("  No test audio file found. Running text-only test...")
            audio_bytes = None

    t1 = time.perf_counter()

    # STT — always ElevenLabs (Scribe), there is no Sarvam STT path in this
    # codebase. If this transcript looks wrong, it's a mic/audio issue,
    # not a provider issue.
    if audio_bytes:
        print("\n📝 Running STT (ElevenLabs Scribe)...")
        t1 = time.perf_counter()
        transcript, language = await transcribe_audio(audio_bytes)
        stt_latency = time.perf_counter() - t1
        print(f"  Transcript: '{transcript}'")
        print(f"  Language: {language}")
        print(f"  Latency: {stt_latency:.3f}s")
    else:
        transcript = f"Tell me about {config.COMPANY_NAME} services"
        language = "en-IN"
        print(f"\n📝 Using test text: '{transcript}'")

    if not transcript:
        print("  ⚠️  Empty transcript. Check your ElevenLabs API key and audio input/mic volume.")
        await close_stt_client()
        await close_tts_client()
        return

    # Agent
    print("\n🧠 Running Agent (GPT-4o)...")
    t2 = time.perf_counter()
    response_text, cost = await process_turn(
        call_id="test-session-001",
        user_text=transcript,
        language=language,
    )
    agent_latency = time.perf_counter() - t2
    print(f"  Response: '{response_text}'")
    print(f"  Cost: ${cost:.5f}")
    print(f"  Latency: {agent_latency:.3f}s")

    # TTS — provider printed here reflects what synthesize_speech() will
    # actually pick, per the live settings check above.
    print(f"\n🔊 Running TTS ({'ElevenLabs' if tts_provider == 'elevenlabs' else 'Sarvam Bulbul v3'})...")
    t3 = time.perf_counter()
    audio_out = await synthesize_speech(response_text, language)
    tts_latency = time.perf_counter() - t3
    print(f"  Audio size: {len(audio_out)} bytes")
    print(f"  Latency: {tts_latency:.3f}s")

    # Play back audio
    if audio_out:
        try:
            import sounddevice as sd
            import numpy as np
            wav_buffer = io.BytesIO(audio_out)
            with wave.open(wav_buffer, "rb") as wf:
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                rate = wf.getframerate()
            print("\n🔈 Playing response audio...")
            sd.play(data, rate)
            sd.wait()
            print("  ✅ Playback complete")
        except Exception as e:
            print(f"  ⚠️  Could not play audio: {e}")
            # Save to file instead
            output_path = os.path.join(os.path.dirname(__file__), "test_output.wav")
            with open(output_path, "wb") as f:
                f.write(audio_out)
            print(f"  ✅ Audio saved to {output_path}")

    print("\n" + "=" * 60)
    total = (time.perf_counter() - t1) if audio_bytes else (agent_latency + tts_latency)
    print(f"  Total pipeline latency: {total:.3f}s")
    print("=" * 60)

    # Close HTTP clients cleanly so asyncio doesn't try to close them
    # during interpreter shutdown (the "Event loop is closed" traceback
    # you saw on exit — harmless, but this removes it).
    await close_stt_client()
    await close_tts_client()


if __name__ == "__main__":
    asyncio.run(test_pipeline())