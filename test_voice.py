"""
test_voice.py — Tests for the pure (Discord-free) logic in nova_voice.

Covers the audio + endpointing helpers that don't need discord, PyNaCl or the
network: PCM conversion, RMS, WAV building, the streaming playback buffer, the
mono->stereo converter, per-speaker utterance endpointing, config parsing and
the speaker-tagging helper.

Run with: python test_voice.py   (stdlib unittest, no extra deps)
"""

from __future__ import annotations

import array
import io
import os
import unittest
import wave

import nova_voice as nv


def _tone(num_samples: int, value: int, channels: int = 1) -> bytes:
    a = array.array("h", [value] * (num_samples * channels))
    return a.tobytes()


class TestPcmHelpers(unittest.TestCase):
    def test_stereo_to_mono_averages_channels(self):
        # interleaved L,R: (1000, 2000) -> 1500 ; (-1000, -2000) -> -1500
        stereo = array.array("h", [1000, 2000, -1000, -2000]).tobytes()
        mono = nv.pcm_stereo_to_mono(stereo)
        samples = array.array("h")
        samples.frombytes(mono)
        self.assertEqual(list(samples), [1500, -1500])

    def test_stereo_to_mono_drops_partial_frame(self):
        # 6 bytes is not a whole stereo frame (4 bytes); trailing 2 bytes dropped
        data = b"\x00\x00\x00\x00\x11"  # 5 bytes
        # must not raise, returns whole-frame mono only
        out = nv.pcm_stereo_to_mono(data)
        self.assertEqual(out, array.array("h", [0]).tobytes())

    def test_stereo_to_mono_empty(self):
        self.assertEqual(nv.pcm_stereo_to_mono(b""), b"")

    def test_rms_silence_is_zero(self):
        self.assertEqual(nv.pcm_rms_int16(_tone(100, 0)), 0.0)

    def test_rms_constant_amplitude(self):
        rms = nv.pcm_rms_int16(_tone(100, 1000))
        self.assertAlmostEqual(rms, 1000.0, places=3)

    def test_rms_empty(self):
        self.assertEqual(nv.pcm_rms_int16(b""), 0.0)


class TestWavBuilder(unittest.TestCase):
    def test_roundtrip(self):
        pcm = _tone(480, 500)  # 10ms mono @ 48k
        wav = nv.build_wav_bytes(pcm, sample_rate=48000, channels=1)
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getframerate(), 48000)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.readframes(w.getnframes()), pcm)


class TestMonoToStereo(unittest.TestCase):
    def test_duplicates_samples(self):
        conv = nv.MonoToStereoConverter()
        mono = array.array("h", [1, 2, 3]).tobytes()
        out = conv.convert(mono)
        self.assertEqual(list(array.array("h", out)), [1, 1, 2, 2, 3, 3])

    def test_handles_odd_byte_chunks(self):
        conv = nv.MonoToStereoConverter()
        full = array.array("h", [7, 8]).tobytes()  # 4 bytes
        # feed 3 bytes then the remaining 1 byte: leftover must be carried over
        first = conv.convert(full[:3])
        second = conv.convert(full[3:])
        combined = list(array.array("h", first + second))
        self.assertEqual(combined, [7, 7, 8, 8])


class TestPCMStreamBuffer(unittest.TestCase):
    def test_emits_full_frames_then_padded_tail(self):
        buf = nv.PCMStreamBuffer(frame_bytes=4)
        buf.feed(b"\x01\x02\x03\x04\x05\x06")  # 1.5 frames
        self.assertEqual(buf.read_frame(), b"\x01\x02\x03\x04")
        # not ended yet, underrun -> silence frame
        self.assertEqual(buf.read_frame(), b"\x00\x00\x00\x00")
        buf.end()
        # padded tail of the leftover 2 bytes
        self.assertEqual(buf.read_frame(), b"\x05\x06\x00\x00")
        # drained + ended -> stop
        self.assertEqual(buf.read_frame(), b"")

    def test_cancel_stops_immediately(self):
        buf = nv.PCMStreamBuffer(frame_bytes=4)
        buf.feed(b"\x01\x02\x03\x04")
        buf.cancel()
        self.assertTrue(buf.cancelled)
        self.assertEqual(buf.read_frame(), b"")
        # feeding after cancel is ignored
        buf.feed(b"\x09\x09\x09\x09")
        self.assertEqual(buf.read_frame(), b"")

    def test_underrun_limit_ends_stream(self):
        buf = nv.PCMStreamBuffer(frame_bytes=4, max_underrun_frames=3)
        for _ in range(3):
            self.assertEqual(buf.read_frame(), b"\x00\x00\x00\x00")
        self.assertEqual(buf.read_frame(), b"")  # exceeded underrun budget


class TestUtteranceCollector(unittest.TestCase):
    def test_endpoints_after_silence(self):
        c = nv.UtteranceCollector()
        c.add(1, "Fede", b"\x10\x10" * 200, now=0.0)
        # still talking (no silence yet)
        self.assertEqual(c.collect_finished(now=0.2, silence_s=0.9, min_bytes=10), [])
        # gone quiet long enough -> finished
        out = c.collect_finished(now=1.0, silence_s=0.9, min_bytes=10)
        self.assertEqual(len(out), 1)
        uid, name, pcm = out[0]
        self.assertEqual((uid, name), (1, "Fede"))
        self.assertEqual(len(pcm), 400)
        # buffer cleared after harvest
        self.assertEqual(c.collect_finished(now=2.0, silence_s=0.9, min_bytes=10), [])

    def test_drops_too_short_utterances(self):
        c = nv.UtteranceCollector()
        c.add(2, "Tizio", b"\x01\x01", now=0.0)  # 2 bytes, below min
        out = c.collect_finished(now=1.0, silence_s=0.5, min_bytes=100)
        self.assertEqual(out, [])

    def test_separate_speakers_kept_apart(self):
        c = nv.UtteranceCollector()
        c.add(1, "A", b"\xaa\xaa" * 100, now=0.0)
        c.add(2, "B", b"\xbb\xbb" * 100, now=0.0)
        out = c.collect_finished(now=1.0, silence_s=0.5, min_bytes=10)
        self.assertEqual({name for _, name, _ in out}, {"A", "B"})


class TestVoiceConfig(unittest.TestCase):
    def setUp(self):
        self._keys = [
            "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
            "ELEVENLABS_OUTPUT_FORMAT", "VOICE_LANGUAGE", "VOICE_STT_PROVIDER",
            "WHISPER_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY",
            "VOICE_SILENCE_MS", "VOICE_MIN_UTTERANCE_MS", "VOICE_ENERGY_THRESHOLD",
            "VOICE_HISTORY_TURNS", "TTS_PROVIDER", "TTS_FALLBACK",
            "CLONE_REFERENCE_WAV", "CLONE_MODEL", "CLONE_LANGUAGE",
        ]
        self._saved = {k: os.environ.get(k) for k in self._keys}
        for k in self._keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults(self):
        # Point the clone reference at a missing file so we test the
        # "nothing configured" path deterministically (independent of the
        # bundled asset).
        os.environ["CLONE_REFERENCE_WAV"] = "/no/such/clone_ref.wav"
        cfg = nv.VoiceConfig.from_env()
        self.assertEqual(cfg.model_id, "eleven_flash_v2_5")
        self.assertEqual(cfg.output_format, "pcm_48000")
        self.assertEqual(cfg.language, "it")
        self.assertEqual(cfg.silence_ms, 900)
        self.assertFalse(cfg.eleven_ready())  # no key/voice
        self.assertFalse(cfg.tts_ready())     # and clone ref missing
        self.assertFalse(cfg.stt_ready())     # no groq key

    def test_eleven_ready_requires_key_and_voice(self):
        os.environ["ELEVENLABS_API_KEY"] = "k"
        self.assertFalse(nv.VoiceConfig.from_env().eleven_ready())
        os.environ["ELEVENLABS_VOICE_ID"] = "v"
        self.assertTrue(nv.VoiceConfig.from_env().eleven_ready())

    def test_stt_ready_per_provider(self):
        os.environ["GROQ_API_KEY"] = "g"
        self.assertTrue(nv.VoiceConfig.from_env().stt_ready())  # default groq
        os.environ.pop("GROQ_API_KEY")
        os.environ["VOICE_STT_PROVIDER"] = "elevenlabs"
        os.environ["ELEVENLABS_API_KEY"] = "k"
        self.assertTrue(nv.VoiceConfig.from_env().stt_ready())

    def test_min_utterance_bytes(self):
        os.environ["VOICE_MIN_UTTERANCE_MS"] = "400"
        cfg = nv.VoiceConfig.from_env()
        # 400ms * 48000 * 2ch * 2bytes / 1000 = 76800
        self.assertEqual(cfg.min_utterance_bytes, 400 * nv.BYTES_PER_MS_STEREO)

    def test_bad_int_falls_back_to_default(self):
        os.environ["VOICE_SILENCE_MS"] = "not-a-number"
        self.assertEqual(nv.VoiceConfig.from_env().silence_ms, 900)

    def test_default_provider_chain(self):
        cfg = nv.VoiceConfig.from_env()
        self.assertEqual(cfg.tts_provider, "elevenlabs")
        self.assertEqual(cfg.tts_fallback, "clone")
        self.assertEqual(cfg.tts_order(), ["elevenlabs", "clone"])

    def test_provider_chain_dedup_and_custom(self):
        os.environ["TTS_PROVIDER"] = "clone"
        os.environ["TTS_FALLBACK"] = "clone"
        self.assertEqual(nv.VoiceConfig.from_env().tts_order(), ["clone"])


class TestTtsReadiness(unittest.TestCase):
    def test_clone_ready_checks_reference_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            cfg = nv.VoiceConfig(clone_reference=f.name)
            self.assertTrue(cfg.clone_ready())
        cfg_missing = nv.VoiceConfig(clone_reference="/no/such/ref.wav")
        self.assertFalse(cfg_missing.clone_ready())

    def test_tts_ready_via_clone_only(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            # No ElevenLabs creds, but clone reference exists and is in the chain
            cfg = nv.VoiceConfig(
                tts_provider="elevenlabs", tts_fallback="clone", clone_reference=f.name
            )
            self.assertFalse(cfg.eleven_ready())
            self.assertTrue(cfg.clone_ready())
            self.assertTrue(cfg.tts_ready())

    def test_tts_ready_via_elevenlabs(self):
        cfg = nv.VoiceConfig(
            elevenlabs_api_key="k", voice_id="v", tts_fallback="", clone_reference="/no/ref.wav"
        )
        self.assertTrue(cfg.eleven_ready())
        self.assertTrue(cfg.tts_ready())

    def test_tts_not_ready_when_nothing_available(self):
        cfg = nv.VoiceConfig(tts_provider="elevenlabs", tts_fallback="clone",
                             clone_reference="/no/ref.wav")
        self.assertFalse(cfg.tts_ready())


class TestCloneGuard(unittest.TestCase):
    def test_synthesize_returns_none_without_reference(self):
        # No reference file -> returns None early, never touches the heavy model
        cfg = nv.VoiceConfig(clone_reference="/no/such/ref.wav")
        self.assertIsNone(nv.clone_synthesize_to_wav("ciao", cfg))


class TestSpeakerTurn(unittest.TestCase):
    def test_format(self):
        self.assertEqual(nv.format_speaker_turn("Fede", "ciao"), "[Fede]: ciao")


class TestFrameConstants(unittest.TestCase):
    def test_frame_bytes_is_20ms_stereo(self):
        self.assertEqual(nv.FRAME_BYTES, 3840)
        self.assertEqual(nv.BYTES_PER_MS_STEREO, 192)


if __name__ == "__main__":
    unittest.main(verbosity=2)
