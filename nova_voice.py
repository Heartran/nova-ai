"""
nova_voice.py — Voice-channel support for Nova (speech in / speech out).

Nova can join a Discord voice channel and hold a spoken conversation while
keeping the EXACT same brain she uses in text: the Claude Agent SDK pipeline
with her personality, memory and all her tools (memory write, server read,
web). Voice is just a different I/O surface on top of the same `call_claude`.

Per spoken turn the flow is:

    Discord voice (per-user Opus)
      --[discord-ext-voice-recv]-->  PCM 48 kHz stereo, attributed to a user
      --[silence endpointing / VAD]--> one finished utterance per speaker
      --[Whisper / ElevenLabs Scribe STT]--> "[DisplayName]: testo"
      --[call_claude(... same memory + tools ...)]--> reply text
      --[ElevenLabs streaming TTS, pcm_48000]--> audio chunks
      --[custom PCM AudioSource]--> played back into the channel

Because each incoming RTP packet is attributed to a Discord member, Nova always
knows *who* is speaking and tags every transcript with the speaker's display
name — exactly like the "[Nome]: ..." convention used for text messages. The
response feels realtime: TTS is streamed and played as it arrives, and Nova
stops talking the moment someone starts speaking over her (barge-in).

DEPENDENCIES
------------
Runtime extras (see requirements.txt): `PyNaCl` (voice encryption) and
`discord-ext-voice-recv` (voice receive). System: a working `libopus` so
incoming Opus can be decoded (discord.py bundles it on Windows/macOS; on Linux
install `libopus0`). With `ELEVENLABS_OUTPUT_FORMAT=pcm_48000` (default) no
ffmpeg is needed; other formats fall back to ffmpeg playback.

The heavy/optional imports (`discord`, `discord.ext.voice_recv`) are performed
LAZILY inside functions, so importing this module only needs the stdlib +
`requests`. That keeps the pure audio/endpointing logic unit-testable without
Discord installed, and lets the rest of the bot run even if the voice extras
are missing — `/voce entra` then replies with a clear setup hint.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import os
import sys
import threading
import time
import wave
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests

logger = logging.getLogger("nova.voice")

# -----------------------------------------------------------------------------
# Audio constants — Discord voice is always 48 kHz, 16-bit, stereo, 20 ms frames
# -----------------------------------------------------------------------------
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes (16-bit)
FRAME_MS = 20
FRAME_BYTES = (SAMPLE_RATE * FRAME_MS // 1000) * CHANNELS * SAMPLE_WIDTH  # 3840
BYTES_PER_MS_STEREO = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH // 1000        # 192


# =============================================================================
# Pure audio helpers (no Discord / no network — unit-testable in isolation)
# =============================================================================
def _as_int16_array(pcm: bytes) -> "array.array":
    """Parse little-endian signed 16-bit PCM into a host-order int16 array."""
    n = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    a = array.array("h")
    a.frombytes(pcm[:n])
    if sys.byteorder == "big":
        a.byteswap()  # Discord PCM is little-endian; normalise to host order
    return a


def _int16_array_to_bytes(a: "array.array") -> bytes:
    if sys.byteorder == "big":
        a = array.array("h", a)
        a.byteswap()
    return a.tobytes()


def pcm_stereo_to_mono(pcm: bytes) -> bytes:
    """Downmix interleaved 16-bit stereo PCM to mono by averaging L/R."""
    if not pcm:
        return b""
    # stereo frames are 4 bytes (L,R); drop any trailing partial frame
    usable = len(pcm) - (len(pcm) % (SAMPLE_WIDTH * CHANNELS))
    samples = _as_int16_array(pcm[:usable])
    out = array.array("h", bytes(SAMPLE_WIDTH * (len(samples) // 2)))
    for i in range(0, len(samples) - 1, 2):
        out[i // 2] = (samples[i] + samples[i + 1]) // 2
    return _int16_array_to_bytes(out)


def pcm_rms_int16(pcm: bytes) -> float:
    """Root-mean-square amplitude of 16-bit PCM (0..32767). Cheap VAD signal."""
    samples = _as_int16_array(pcm)
    if not len(samples):
        return 0.0
    acc = 0
    for v in samples:
        acc += v * v
    return math.sqrt(acc / len(samples))


def build_wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container (for upload to an STT API)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class MonoToStereoConverter:
    """Streaming mono->stereo (sample duplication), tolerant of odd byte chunks.

    ElevenLabs `pcm_48000` is mono; Discord wants stereo. Chunks from an HTTP
    stream don't necessarily end on a sample boundary, so we keep the leftover
    byte across calls.
    """

    def __init__(self) -> None:
        self._rem = b""

    def convert(self, data: bytes) -> bytes:
        buf = self._rem + data
        usable = len(buf) - (len(buf) % SAMPLE_WIDTH)
        body, self._rem = buf[:usable], buf[usable:]
        if not body:
            return b""
        mono = _as_int16_array(body)
        stereo = array.array("h", bytes(SAMPLE_WIDTH * CHANNELS * len(mono)))
        for i, v in enumerate(mono):
            stereo[2 * i] = v
            stereo[2 * i + 1] = v
        return _int16_array_to_bytes(stereo)


class PCMStreamBuffer:
    """Thread-safe ring of PCM bytes that yields fixed 20 ms frames to Discord.

    The producer thread (ElevenLabs HTTP stream) calls `feed()`; Discord's audio
    thread calls `read_frame()` every 20 ms. `read_frame` returns:
      - a full frame when enough data is buffered,
      - a silence frame while waiting for more (up to `max_underrun_frames`),
      - the last padded frame, then b"" once the stream has `end()`ed,
      - b"" immediately after `cancel()` (barge-in).
    """

    def __init__(self, frame_bytes: int = FRAME_BYTES, max_underrun_frames: int = 250) -> None:
        self._frame = frame_bytes
        self._buf = bytearray()
        self._ended = False
        self._cancelled = False
        self._underruns = 0
        self._max_underrun = max_underrun_frames
        self._silence = bytes(frame_bytes)
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if not self._cancelled:
                self._buf.extend(data)

    def end(self) -> None:
        with self._lock:
            self._ended = True

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._ended = True
            self._buf.clear()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def read_frame(self) -> bytes:
        with self._lock:
            if len(self._buf) >= self._frame:
                self._underruns = 0
                frame = bytes(self._buf[: self._frame])
                del self._buf[: self._frame]
                return frame
            if self._ended:
                if self._buf:
                    frame = bytes(self._buf).ljust(self._frame, b"\x00")
                    self._buf.clear()
                    return frame
                return b""
            # Producer is behind: emit silence briefly rather than cutting out.
            self._underruns += 1
            if self._underruns > self._max_underrun:
                return b""
            return self._silence


class UtteranceCollector:
    """Per-speaker PCM accumulation with silence-based endpointing.

    `add()` is called from the voice-receive thread for every packet; the bot's
    event loop periodically calls `collect_finished()` to harvest utterances
    that have gone quiet for `silence_s`. Each result is one speaker's full
    utterance, ready for transcription.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # uid -> {"buf": bytearray, "name": str, "last": float}
        self._data: dict[int, dict] = {}

    def add(self, uid: int, name: str, pcm: bytes, now: float) -> None:
        with self._lock:
            entry = self._data.get(uid)
            if entry is None:
                entry = {"buf": bytearray(), "name": name, "last": now}
                self._data[uid] = entry
            entry["buf"].extend(pcm)
            entry["name"] = name
            entry["last"] = now

    def collect_finished(self, now: float, silence_s: float, min_bytes: int) -> list[tuple[int, str, bytes]]:
        finished: list[tuple[int, str, bytes]] = []
        with self._lock:
            for uid in list(self._data.keys()):
                entry = self._data[uid]
                if entry["buf"] and (now - entry["last"]) >= silence_s:
                    pcm = bytes(entry["buf"])
                    del self._data[uid]
                    if len(pcm) >= min_bytes:
                        finished.append((uid, entry["name"], pcm))
        return finished

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def format_speaker_turn(name: str, text: str) -> str:
    """Tag a transcript with the speaker, mirroring the text "[Nome]: ..." format."""
    return f"[{name}]: {text}".strip()


# =============================================================================
# Configuration
# =============================================================================
@dataclass
class VoiceConfig:
    """Voice settings, read from the environment (see .env.example)."""

    elevenlabs_api_key: str = ""
    voice_id: str = ""
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "pcm_48000"
    language: str = "it"
    # STT: empty -> reuse the existing Whisper pipeline (WHISPER_PROVIDER);
    # may also be "elevenlabs" to use ElevenLabs Scribe.
    stt_provider: str = ""
    whisper_provider: str = "groq"
    groq_api_key: str = ""
    openai_api_key: str = ""
    # Endpointing / barge-in
    silence_ms: int = 900
    min_utterance_ms: int = 400
    energy_threshold: int = 350
    history_turns: int = 8

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
            voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
            model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip(),
            output_format=os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_48000").strip(),
            language=os.getenv("VOICE_LANGUAGE", "it").strip(),
            stt_provider=os.getenv("VOICE_STT_PROVIDER", "").strip().lower(),
            whisper_provider=os.getenv("WHISPER_PROVIDER", "groq").strip().lower(),
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            silence_ms=_int("VOICE_SILENCE_MS", 900),
            min_utterance_ms=_int("VOICE_MIN_UTTERANCE_MS", 400),
            energy_threshold=_int("VOICE_ENERGY_THRESHOLD", 350),
            history_turns=_int("VOICE_HISTORY_TURNS", 8),
        )

    def tts_ready(self) -> bool:
        return bool(self.elevenlabs_api_key and self.voice_id)

    def stt_ready(self) -> bool:
        provider = self.stt_provider or self.whisper_provider
        if provider == "elevenlabs":
            return bool(self.elevenlabs_api_key)
        if provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.groq_api_key)  # groq default

    @property
    def min_utterance_bytes(self) -> int:
        return max(0, self.min_utterance_ms) * BYTES_PER_MS_STEREO


# =============================================================================
# Speech-to-text (ElevenLabs Scribe, or reuse the Whisper pipeline via Groq/OpenAI)
# =============================================================================
def transcribe_wav_bytes(wav_bytes: bytes, config: VoiceConfig) -> Optional[str]:
    """Transcribe a WAV payload. Mirrors nova_whatsapp.transcribe_audio_file but
    works on in-memory bytes and adds an ElevenLabs Scribe option."""
    provider = config.stt_provider or config.whisper_provider
    try:
        if provider == "elevenlabs":
            return _stt_elevenlabs(wav_bytes, config)
        return _stt_whisper(wav_bytes, config, provider)
    except requests.RequestException as e:
        logger.error("voice STT request error (%s): %s", provider, e)
        return None
    except Exception:  # pragma: no cover - defensive
        logger.exception("voice STT unexpected error (%s)", provider)
        return None


def _stt_whisper(wav_bytes: bytes, config: VoiceConfig, provider: str) -> Optional[str]:
    if provider == "openai":
        api_key, url, model = config.openai_api_key, "https://api.openai.com/v1/audio/transcriptions", "whisper-1"
    else:  # groq default
        api_key = config.groq_api_key
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        model = "whisper-large-v3-turbo"
    if not api_key:
        logger.warning("voice STT: no API key for provider=%s", provider)
        return None

    data = {"model": model, "response_format": "text"}
    if config.language:
        data["language"] = config.language
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=60,
    )
    if resp.status_code == 200:
        return resp.text.strip()
    logger.error("voice STT (%s) HTTP %s: %s", provider, resp.status_code, resp.text[:300])
    return None


def _stt_elevenlabs(wav_bytes: bytes, config: VoiceConfig) -> Optional[str]:
    if not config.elevenlabs_api_key:
        logger.warning("voice STT: ELEVENLABS_API_KEY missing for Scribe")
        return None
    data = {"model_id": "scribe_v1"}
    if config.language:
        data["language_code"] = config.language
    resp = requests.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": config.elevenlabs_api_key},
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        data=data,
        timeout=60,
    )
    if resp.status_code == 200:
        return (resp.json().get("text") or "").strip()
    logger.error("voice STT (elevenlabs) HTTP %s: %s", resp.status_code, resp.text[:300])
    return None


# =============================================================================
# Text-to-speech (ElevenLabs streaming)
# =============================================================================
def _tts_url(config: VoiceConfig) -> str:
    return (
        f"https://api.elevenlabs.io/v1/text-to-speech/{config.voice_id}/stream"
        f"?output_format={config.output_format}"
    )


def _tts_payload(text: str, config: VoiceConfig) -> dict:
    payload = {
        "text": text,
        "model_id": config.model_id,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
    }
    if config.language:
        payload["language_code"] = config.language
    return payload


def eleven_tts_stream(text: str, config: VoiceConfig) -> Iterator[bytes]:
    """Yield raw audio chunks from ElevenLabs streaming TTS (PCM output)."""
    resp = requests.post(
        _tts_url(config),
        headers={"xi-api-key": config.elevenlabs_api_key, "Content-Type": "application/json"},
        json=_tts_payload(text, config),
        stream=True,
        timeout=60,
    )
    if resp.status_code != 200:
        body = resp.text[:300] if hasattr(resp, "text") else ""
        logger.error("voice TTS HTTP %s: %s", resp.status_code, body)
        resp.close()
        return
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
    finally:
        resp.close()


def eleven_tts_bytes(text: str, config: VoiceConfig) -> Optional[bytes]:
    """Fetch the full TTS audio (used for non-PCM formats played via ffmpeg)."""
    chunks = list(eleven_tts_stream(text, config))
    return b"".join(chunks) if chunks else None


# =============================================================================
# Optional-dependency probe
# =============================================================================
def voice_deps_available() -> tuple[bool, str]:
    """Return (ok, hint). False when the voice extras are not importable."""
    try:
        import nacl  # noqa: F401  (PyNaCl, required for voice encryption)
    except Exception:
        return False, "Manca PyNaCl. Installa con: pip install PyNaCl"
    try:
        from discord.ext import voice_recv  # noqa: F401
    except Exception:
        return False, "Manca discord-ext-voice-recv. Installa con: pip install discord-ext-voice-recv"
    return True, ""


# =============================================================================
# Discord-bound pieces (lazy: built only when actually joining a channel)
# =============================================================================
def _make_audio_source(buffer: PCMStreamBuffer):
    """Wrap a PCMStreamBuffer in a discord.AudioSource (imported lazily)."""
    import discord

    class _PCMStreamSource(discord.AudioSource):
        def is_opus(self) -> bool:
            return False

        def read(self) -> bytes:
            return buffer.read_frame()

        def cleanup(self) -> None:  # pragma: no cover - discord-driven
            buffer.cancel()

    return _PCMStreamSource()


def _build_sink(on_packet: Callable[[object, bytes], None]):
    """Build a voice_recv AudioSink that forwards decoded PCM per user."""
    from discord.ext import voice_recv

    class _NovaSink(voice_recv.AudioSink):
        def wants_opus(self) -> bool:
            return False  # we want decoded PCM, not raw Opus

        def write(self, user, data) -> None:  # pragma: no cover - thread/IO
            if user is None or getattr(user, "bot", False):
                return
            pcm = getattr(data, "pcm", None)
            if pcm:
                on_packet(user, pcm)

        def cleanup(self) -> None:  # pragma: no cover
            pass

    return _NovaSink()


# Brain callback: given the running conversation + context, return Nova's reply.
BrainFn = Callable[..., "asyncio.Future[str]"]


class VoiceSession:
    """One live voice conversation in a single guild's voice channel."""

    def __init__(
        self,
        client,
        guild,
        channel,
        config: VoiceConfig,
        scope_dir: Path,
        brain_fn: BrainFn,
    ) -> None:
        self.client = client
        self.guild = guild
        self.channel = channel
        self.config = config
        self.scope_dir = scope_dir
        self.brain_fn = brain_fn

        self._vc = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._collector = UtteranceCollector()
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._turns: deque = deque(maxlen=max(2, config.history_turns * 2))
        self._running = False
        self._endpoint_task: Optional[asyncio.Task] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._play_buffer: Optional[PCMStreamBuffer] = None

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        from discord.ext import voice_recv

        self._loop = asyncio.get_running_loop()
        self._vc = await self.channel.connect(cls=voice_recv.VoiceRecvClient)
        self._vc.listen(_build_sink(self._on_packet))
        self._running = True
        self._endpoint_task = asyncio.create_task(self._endpoint_loop())
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("voice: joined #%s in %s", getattr(self.channel, "name", "?"), self.guild)

    async def stop(self) -> None:
        self._running = False
        if self._play_buffer:
            self._play_buffer.cancel()
        if self._endpoint_task:
            self._endpoint_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._endpoint_task
        if self._worker_task:
            self._queue.put_nowait(None)
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
        if self._vc is not None:
            with suppress(Exception):
                self._vc.stop()
            if hasattr(self._vc, "stop_listening"):
                with suppress(Exception):
                    self._vc.stop_listening()
            with suppress(Exception):
                await self._vc.disconnect()
        self._collector.clear()
        logger.info("voice: left voice channel in %s", self.guild)

    # -- inbound audio ------------------------------------------------------
    def _on_packet(self, user, pcm: bytes) -> None:
        """Called from the voice-receive thread for each decoded packet."""
        name = getattr(user, "display_name", None) or getattr(user, "name", "ignoto")
        self._collector.add(user.id, name, pcm, time.monotonic())

        # Barge-in: if Nova is talking and a human starts speaking, yield.
        vc = self._vc
        if vc is not None and vc.is_playing() and pcm_rms_int16(pcm) >= self.config.energy_threshold:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._barge_in)

    def _barge_in(self) -> None:
        if self._play_buffer is not None:
            self._play_buffer.cancel()
        if self._vc is not None and self._vc.is_playing():
            with suppress(Exception):
                self._vc.stop()

    async def _endpoint_loop(self) -> None:
        silence_s = self.config.silence_ms / 1000.0
        min_bytes = self.config.min_utterance_bytes
        while self._running:
            await asyncio.sleep(0.1)
            for uid, name, pcm in self._collector.collect_finished(time.monotonic(), silence_s, min_bytes):
                self._queue.put_nowait((uid, name, pcm))

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    break
                uid, name, pcm = item
                await self._process_utterance(name, pcm)
            except Exception:  # pragma: no cover - defensive
                logger.exception("voice: error handling utterance")
            finally:
                self._queue.task_done()

    async def _process_utterance(self, speaker: str, pcm_stereo: bytes) -> None:
        wav = build_wav_bytes(pcm_stereo_to_mono(pcm_stereo), SAMPLE_RATE, channels=1)
        text = await asyncio.to_thread(transcribe_wav_bytes, wav, self.config)
        if not text:
            return
        logger.info("voice <- %s", format_speaker_turn(speaker, text[:120]))

        self._turns.append({"role": "user", "content": format_speaker_turn(speaker, text)})
        requester = f"{speaker} (voce)"
        reply = await self.brain_fn(
            list(self._turns), self.guild, self.scope_dir, requester
        )
        if not reply:
            return
        self._turns.append({"role": "assistant", "content": reply})
        logger.info("voice -> Nova: %s", reply[:120])
        await self._speak(reply)

    # -- outbound audio -----------------------------------------------------
    async def _speak(self, text: str) -> None:
        if self._vc is None or not self.config.tts_ready():
            if not self.config.tts_ready():
                logger.warning("voice: TTS not configured (ELEVENLABS_API_KEY/VOICE_ID); cannot speak")
            return
        if self.config.output_format.startswith("pcm"):
            await self._speak_pcm(text)
        else:
            await self._speak_ffmpeg(text)

    async def _speak_pcm(self, text: str) -> None:
        buffer = PCMStreamBuffer(FRAME_BYTES)
        self._play_buffer = buffer
        source = _make_audio_source(buffer)
        done = asyncio.Event()

        def _after(err) -> None:
            if err:
                logger.warning("voice: playback error: %s", err)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(done.set)

        producer = asyncio.create_task(self._produce_tts(text, buffer))
        try:
            self._vc.play(source, after=_after)
        except Exception:
            logger.exception("voice: failed to start playback")
            buffer.cancel()
            await producer
            self._play_buffer = None
            return

        await done.wait()
        buffer.end()
        with suppress(Exception):
            await producer
        self._play_buffer = None

    async def _produce_tts(self, text: str, buffer: PCMStreamBuffer) -> None:
        converter = MonoToStereoConverter()

        def _run() -> None:
            for chunk in eleven_tts_stream(text, self.config):
                if buffer.cancelled:
                    break
                buffer.feed(converter.convert(chunk))

        try:
            await asyncio.to_thread(_run)
        finally:
            buffer.end()

    async def _speak_ffmpeg(self, text: str) -> None:
        import tempfile

        import discord

        audio = await asyncio.to_thread(eleven_tts_bytes, text, self.config)
        if not audio:
            return
        suffix = ".mp3" if "mp3" in self.config.output_format else ".audio"
        fd, path = tempfile.mkstemp(prefix="nova_tts_", suffix=suffix)
        os.close(fd)
        Path(path).write_bytes(audio)
        done = asyncio.Event()

        def _after(err) -> None:
            if err:
                logger.warning("voice: playback error: %s", err)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(done.set)

        try:
            self._vc.play(discord.FFmpegPCMAudio(path), after=_after)
            await done.wait()
        except Exception:
            logger.exception("voice: ffmpeg playback failed (is ffmpeg installed?)")
        finally:
            with suppress(OSError):
                os.unlink(path)


class VoiceManager:
    """Tracks one VoiceSession per guild and wires it to Nova's brain."""

    def __init__(
        self,
        client,
        config: VoiceConfig,
        brain_fn: BrainFn,
        base_dir: Path,
        scope_dir_for: Callable[[Path, str, int], Path],
        ensure_skeleton: Callable[[Path, str], None],
    ) -> None:
        self.client = client
        self.config = config
        self.brain_fn = brain_fn
        self.base_dir = base_dir
        self.scope_dir_for = scope_dir_for
        self.ensure_skeleton = ensure_skeleton
        self._sessions: dict[int, VoiceSession] = {}

    def session_for(self, guild_id: int) -> Optional[VoiceSession]:
        return self._sessions.get(guild_id)

    async def join(self, guild, channel) -> tuple[bool, str]:
        ok, hint = voice_deps_available()
        if not ok:
            return False, f"Non posso entrare in vocale: {hint}"
        if not self.config.stt_ready():
            return False, (
                "Manca la configurazione per ascoltare (STT). Imposta GROQ_API_KEY "
                "(o VOICE_STT_PROVIDER) nel .env."
            )

        existing = self._sessions.get(guild.id)
        if existing is not None:
            with suppress(Exception):
                await existing.stop()

        scope_dir = self.scope_dir_for(self.base_dir, "server", guild.id)
        self.ensure_skeleton(scope_dir, "server")

        session = VoiceSession(
            self.client, guild, channel, self.config, scope_dir, self.brain_fn
        )
        try:
            await session.start()
        except Exception as e:
            logger.exception("voice: join failed")
            with suppress(Exception):
                await session.stop()
            return False, f"Non sono riuscita a entrare: {e}"

        self._sessions[guild.id] = session
        msg = f"Entrata in #{getattr(channel, 'name', '?')}."
        if not self.config.tts_ready():
            msg += " Attenzione: TTS non configurato, ti sento ma non parlo (ELEVENLABS_API_KEY/VOICE_ID)."
        return True, msg

    async def leave(self, guild_id: int) -> tuple[bool, str]:
        session = self._sessions.pop(guild_id, None)
        if session is None:
            return False, "Non sono in nessun canale vocale qui."
        with suppress(Exception):
            await session.stop()
        return True, "Uscita dal canale vocale."

    def status_text(self, guild_id: int) -> str:
        session = self._sessions.get(guild_id)
        if session is None:
            return "Non sono in vocale in questo server."
        return f"Sono in #{getattr(session.channel, 'name', '?')}, in ascolto."

    async def shutdown(self) -> None:
        for gid in list(self._sessions.keys()):
            await self.leave(gid)


def register_voice_commands(tree, manager: VoiceManager) -> None:
    """Register the /voce slash command group. discord is imported lazily."""
    import discord
    from discord import app_commands

    voce = app_commands.Group(name="voce", description="Gestione del canale vocale di Nova")

    @voce.command(name="entra", description="Fai entrare Nova nel tuo canale vocale")
    async def voce_entra(interaction: "discord.Interaction"):
        if interaction.guild is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        member = interaction.user
        voice_state = getattr(member, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Devi prima entrare tu in un canale vocale.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manager.join(interaction.guild, voice_state.channel)
        await interaction.followup.send(msg, ephemeral=True)

    @voce.command(name="esci", description="Fai uscire Nova dal canale vocale")
    async def voce_esci(interaction: "discord.Interaction"):
        if interaction.guild is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        _, msg = await manager.leave(interaction.guild.id)
        await interaction.response.send_message(msg, ephemeral=True)

    @voce.command(name="stato", description="Mostra se Nova e' in un canale vocale")
    async def voce_stato(interaction: "discord.Interaction"):
        if interaction.guild is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        await interaction.response.send_message(
            manager.status_text(interaction.guild.id), ephemeral=True
        )

    tree.add_command(voce)
