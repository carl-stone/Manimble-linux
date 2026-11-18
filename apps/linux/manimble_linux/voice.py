"""Native and account-backed narration. Blocking methods run only in workers.

Credentials never enter a deck, a preference, a cache key, or a worker job. The
Secret Service backend is selected explicitly rather than keyring's fallback chain.
"""
from __future__ import annotations

from concurrent.futures import Future
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class VoiceError(RuntimeError):
    pass


class VoiceCancelled(VoiceError):
    pass


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def audio_duration(path: Path) -> float:
    import av
    with av.open(str(path)) as source:
        stream = next(iter(source.streams.audio), None)
        if stream is None:
            raise VoiceError(f"No audio in {path}")
        if stream.duration is not None:
            return float(stream.duration * stream.time_base)
        return sum(frame.samples / frame.sample_rate for frame in source.decode(stream))


def secret_backend():
    try:
        from keyring.backends.SecretService import Keyring
        backend = Keyring()
        # Accessing priority actually checks Secret Service availability.
        if backend.priority <= 0:
            raise VoiceError("Secret Service is unavailable")
        return backend
    except Exception as exc:
        raise VoiceError(f"Cannot unlock the Secret Service keyring: {exc}") from exc


class CloudVoiceService(str, Enum):
    elevenlabs = "elevenlabs"
    openai = "openai"

    @property
    def name(self):
        return "ElevenLabs" if self is self.elevenlabs else "OpenAI"

    @property
    def key_page(self):
        return ("https://elevenlabs.io/app/settings/api-keys" if self is self.elevenlabs
                else "https://platform.openai.com/api-keys")

    @property
    def builtin_voices(self):
        if self is self.elevenlabs:
            return [("21m00Tcm4TlvDq8ikWAM", "Rachel"), ("pNInz6obpgDQGcFmaJgB", "Adam"),
                    ("EXAVITQu4vr4xnSDxMaL", "Sarah"), ("ErXwobaYiN019PkySvjV", "Antoni"),
                    ("TxGEqnHWrfWFTfGW9XjX", "Josh"), ("XrExE9yKIg1WjnnlVkGX", "Matilda")]
        return [(v, v.capitalize()) for v in
                ("alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer")]

    @property
    def api_key(self):
        try:
            return secret_backend().get_password("org.pachterlab.Manimble.voice", self.value)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"Cannot read the Secret Service keyring: {exc}") from exc

    @property
    def connected(self):
        return bool(self.api_key)

    def set_key(self, key):
        try:
            backend = secret_backend()
            if key:
                backend.set_password("org.pachterlab.Manimble.voice", self.value, key)
            elif backend.get_password("org.pachterlab.Manimble.voice", self.value) is not None:
                backend.delete_password("org.pachterlab.Manimble.voice", self.value)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"Cannot update the Secret Service keyring: {exc}") from exc

    def verify(self, key, *, base_url=None):
        return self._list(key, base_url=base_url)

    def refresh_voices(self, *, base_url=None):
        key = self.api_key
        if not key:
            raise VoiceError(f"{self.name} is not connected.")
        data = self._list(key, base_url=base_url)
        if self is self.openai:
            return self.builtin_voices
        return sorted([(v["voice_id"], v["name"]) for v in data["voices"]], key=lambda v: v[1])

    def _list(self, key, *, base_url=None):
        base = base_url or ("https://api.elevenlabs.io" if self is self.elevenlabs else "https://api.openai.com")
        endpoint = "/v1/voices" if self is self.elevenlabs else "/v1/models"
        headers = {"xi-api-key": key} if self is self.elevenlabs else {"Authorization": "Bearer " + key}
        return json.loads(http_bytes(Request(base + endpoint, headers=headers)))


def http_bytes(request):
    try:
        with urlopen(request, timeout=60) as reply:
            return reply.read()
    except HTTPError as exc:
        with exc:
            body = exc.read(400).decode("utf-8", "replace")
        raise VoiceError("The key was refused (401)." if exc.code == 401 else f"HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise VoiceError(f"Speech service request failed: {exc}") from exc


class CloudVoice:
    _in_flight: dict[Path, Future] = {}
    _lock = threading.Lock()

    def __init__(self, identifier=None, *, service=None, voice=None, cache_dir=None, base_url=None):
        if service is not None:
            self.service, self.voice = CloudVoiceService(service), voice
        else:
            parts = (identifier or "").split(":", 2)
            if len(parts) != 3 or parts[0] != "cloud" or not parts[2]:
                raise ValueError("Not a cloud voice identifier")
            self.service, self.voice = CloudVoiceService(parts[1]), parts[2]
        if not self.voice:
            raise ValueError("Missing cloud voice")
        if cache_dir is None:
            from .paths import AppPaths
            cache_dir = AppPaths.from_environment().voice_cache
        self.cache_dir, self.base_url = Path(cache_dir), base_url

    @classmethod
    def parse(cls, identifier, **kwargs):
        try:
            return cls(identifier, **kwargs)
        except ValueError:
            return None

    @property
    def identifier(self):
        return f"cloud:{self.service.value}:{self.voice}"

    def speed(self, rate):
        lo, hi = (0.7, 1.2) if self.service is CloudVoiceService.elevenlabs else (0.25, 4.0)
        return min(hi, max(lo, rate / 0.5))

    def cache_file(self, text, rate):
        key = f"{self.identifier}|{self.speed(rate):.3f}|{text}"
        return self.cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".mp3")

    def audio(self, text, rate=0.5):
        """Blocking; caller must obtain disclosure consent before a cold request."""
        file = self.cache_file(text, rate)
        if file.is_file():
            return file
        with self._lock:
            existing = self._in_flight.get(file)
            if existing is None:
                pending = Future()
                self._in_flight[file] = pending
        if existing is not None:
            return existing.result()
        try:
            result = self.fetch(text, rate, file)
            pending.set_result(result)
            return result
        except BaseException as exc:
            pending.set_exception(exc)
            # The owning caller receives the same failure; consume the Future too.
            pending.exception()
            raise
        finally:
            with self._lock:
                self._in_flight.pop(file, None)

    def fetch(self, text, rate, file):
        key = self.service.api_key
        if not key:
            raise VoiceError(f"{self.service.name} is not connected.")
        if self.service is CloudVoiceService.elevenlabs:
            base = self.base_url or "https://api.elevenlabs.io"
            endpoint = "/v1/text-to-speech/" + quote(self.voice, safe="") + "?output_format=mp3_44100_128"
            headers = {"xi-api-key": key}
            body = {"text": text, "model_id": "eleven_multilingual_v2", "voice_settings": {"speed": self.speed(rate)}}
        else:
            base = self.base_url or "https://api.openai.com"
            endpoint = "/v1/audio/speech"
            headers = {"Authorization": "Bearer " + key}
            body = {"model": "tts-1-hd", "voice": self.voice, "input": text,
                    "speed": self.speed(rate), "response_format": "mp3"}
        headers["Content-Type"] = "application/json"
        data = http_bytes(Request(base + endpoint, data=json.dumps(body).encode("utf-8"), headers=headers))
        if not data:
            raise VoiceError("The speech service returned empty audio.")
        atomic_bytes(file, data)
        return file


class NativeVoice:
    prefix = "linux:espeak-ng:"

    def __init__(self, cache_dir, env=None):
        self.cache_dir, self.env = Path(cache_dir), env

    def voices(self):
        executable = shutil.which("espeak-ng", path=(self.env or os.environ).get("PATH"))
        if not executable:
            raise VoiceError("eSpeak NG is not installed. Install the espeak-ng native package to use Linux narration.")
        reply = subprocess.run([executable, "--voices"], capture_output=True, text=True, env=self.env, timeout=15, check=True)
        rows = []
        for row in reply.stdout.splitlines()[1:]:
            fields = row.split()
            if len(fields) >= 5:
                rows.append((self.prefix + fields[4], fields[3].replace("_", " ") + " · " + fields[1]))
        return rows

    def audio(self, text, voice=None, rate=0.5, cancel_event=None):
        available = dict(self.voices())
        warning = None
        chosen = voice
        if voice and voice not in available:
            chosen = None
            warning = f"Voice {voice!r} is unavailable on Linux; using the default Linux voice. The saved identifier is unchanged."
        wpm = round(175 * rate / 0.5)
        identifier = chosen or self.prefix + "default"
        digest = hashlib.sha256(f"{identifier}|{wpm}|{text}".encode("utf-8")).hexdigest()
        file = self.cache_dir / (digest + ".wav")
        if not file.is_file():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".speech-", suffix=".wav", dir=self.cache_dir)
            os.close(fd)
            try:
                command = [shutil.which("espeak-ng", path=(self.env or os.environ).get("PATH")),
                           "--stdin", "-s", str(wpm), "-w", name]
                if chosen:
                    command += ["-v", chosen[len(self.prefix):]]
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env=self.env)
                try:
                    first = True
                    deadline = time.monotonic() + 60
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise VoiceCancelled("Narration cancelled")
                        if time.monotonic() >= deadline:
                            raise VoiceError("eSpeak NG synthesis timed out after 60 seconds")
                        try:
                            stdout, stderr = process.communicate(text.encode("utf-8") if first else None, timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            first = False
                    if process.returncode:
                        raise VoiceError(stderr.decode("utf-8", "replace") or "eSpeak NG synthesis failed")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                audio_duration(Path(name))
                os.replace(name, file)
            finally:
                Path(name).unlink(missing_ok=True)
        return file, warning


class VoiceManager:
    """GUI continuation boundary shared by previews, presentations and movie preparation."""
    def __init__(self, cache_dir, parent=None, env=None, warn=None):
        self.cache_dir, self.parent, self.env = Path(cache_dir), parent, env
        self.warn = warn or (lambda text: None)
        self._approved = set()
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    def _check_cancelled(self, token=None):
        if (token or self._cancelled).is_set():
            raise VoiceCancelled("Narration cancelled")

    def speech(self, text, voice=None, rate=0.5):
        from .workers import run_blocking
        from PySide6.QtWidgets import QMessageBox
        if self._cancelled.is_set():
            self._cancelled = threading.Event()
        token = self._cancelled
        text = text.strip()
        if not text:
            return None
        cloud = CloudVoice.parse(voice, cache_dir=self.cache_dir)
        if cloud is not None:
            warm = yield run_blocking(cloud.cache_file(text, rate).is_file)
            self._check_cancelled(token)
            if not warm and cloud.service not in self._approved:
                answer = QMessageBox.question(self.parent, "Online narration",
                    f"Send script text to {cloud.service.name} for speech synthesis? This discloses the text to that provider "
                    "and may incur charges. This approval applies to this voice/play/export action.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if answer != QMessageBox.StandardButton.Yes:
                    raise VoiceCancelled("Online narration cancelled")
                self._approved.add(cloud.service)
            try:
                path = yield run_blocking(cloud.audio, text, rate)
                duration = yield run_blocking(audio_duration, path)
                self._check_cancelled(token)
                return path, duration
            except Exception as exc:
                self._check_cancelled(token)
                dialog = QMessageBox(self.parent)
                dialog.setWindowTitle("Online narration failed")
                dialog.setText(str(exc))
                native = dialog.addButton("Use Linux Voice", QMessageBox.ButtonRole.AcceptRole)
                cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
                dialog.setDefaultButton(cancel)
                dialog.exec()
                if dialog.clickedButton() is not native:
                    raise VoiceCancelled(str(exc)) from exc
                voice = None
        native = NativeVoice(self.cache_dir, self.env)
        path, warning = yield run_blocking(native.audio, text, voice, rate, token)
        self._check_cancelled(token)
        if warning:
            self.warn(warning)
        duration = yield run_blocking(audio_duration, path)
        self._check_cancelled(token)
        return path, duration


class CloudVoiceAccount:
    @staticmethod
    def connect(service, parent=None):
        from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox
        from .workers import run_blocking
        service = CloudVoiceService(service)
        key, ok = QInputDialog.getText(parent, f"Connect {service.name}",
            f"API key ({service.key_page}). Stored only in the Secret Service keyring.\nLeave empty to disconnect.",
            QLineEdit.EchoMode.Password)
        if not ok:
            return None
        if not key:
            answer = QMessageBox.question(parent, "Disconnect account", f"Remove the saved {service.name} key?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return None
        if key:
            yield run_blocking(service.verify, key)
        yield run_blocking(service.set_key, key.strip() or None)
        return (yield run_blocking(service.refresh_voices)) if key else service.builtin_voices
