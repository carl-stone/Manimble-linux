"""Portable data/math/scheduling tests; HTTP is restricted to a local fixture."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from manimble_linux.script import ScriptClock, ScriptMotion, ScriptPerformer, ScriptRunner, ScriptStep, ScriptStore
from manimble_linux.script_movie import PointerTimeline, ScriptMovieExport, audio_blocks, timeline
from manimble_linux.voice import CloudVoice, CloudVoiceService, NativeVoice, VoiceError


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="manimble-script-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class StoreTests(TempCase):
    def store(self, original=False):
        return ScriptStore(self.root / "script.json", original)

    def test_defaults_and_new_empty_removal(self):
        store = self.store()
        self.assertEqual((store.voice, store.rate, store.hold, store.is_empty), (None, 0.5, 3, True))
        store.save()
        self.assertFalse(store.url.exists())
        step = store.add("speak", "Welcome")
        store.remove(step.id)
        self.assertFalse(store.url.exists())
        store.set_rate(0.6)
        self.assertTrue(store.url.is_file())
        store.set_rate(0.5)
        self.assertFalse(store.url.exists())

    def test_original_empty_is_preserved(self):
        path = self.root / "script.json"
        path.write_text('{"version":1,"steps":[]}', encoding="utf-8")
        store = self.store(True)
        step = store.add("pause", "Welcome")
        store.remove(step.id)
        self.assertEqual(json.loads(path.read_text()), {"version": 1, "rate": 0.5, "hold": 3.0, "steps": []})
        path.write_bytes(b"")
        store = self.store(True)
        store.save()
        self.assertTrue(path.exists())

    def test_foreign_voice_uuid_order_and_all_fields_round_trip(self):
        for voice in ("com.apple.voice.enhanced.en-US.Ava", "cloud:elevenlabs:future-voice", "vendor:unknown:voice"):
            store = self.store()
            store.set_voice(voice)
            store.set_rate(0.63)
            store.set_hold(4.2)
            store.steps = [ScriptStep("A-UUID-UNCHANGED", "Welcome", 2, "move", "", 1, [[0.1, 0.2], [0.9, 0.8]], 1, 0.4),
                           ScriptStep("B-UUID-UNCHANGED", "Welcome", 2, "speak", "α 😀\nsecond line")]
            store.save()
            restored = self.store()
            self.assertEqual((restored.voice, restored.rate, restored.hold), (voice, 0.63, 4.2))
            self.assertEqual(restored.steps, store.steps)
            self.assertEqual(restored.steps[0].id, "A-UUID-UNCHANGED")
            self.assertEqual(set(json.loads(store.url.read_text())["steps"][0]),
                             {"id", "scene", "build", "kind", "text", "seconds", "path", "speed", "noise"})

    def test_missing_optional_step_fields(self):
        step = ScriptStep.from_dict({"id": "uuid", "scene": "Welcome", "kind": "pause"})
        self.assertEqual((step.build, step.text, step.seconds, step.path, step.speed, step.noise), (0, "", 1, [], 0.5, 0))

    def test_add_shift_remove_and_pointer(self):
        store = self.store()
        a = store.add("move", "A")
        store.update(a.id, path=[[0.2, 0.3]], speed=1, noise=0.8)
        b = store.add("speak", "B")
        c = store.add("move", "A")
        self.assertEqual((c.speed, c.noise), (1, 0.8))
        self.assertEqual([s.id for s in store.steps], [a.id, c.id, b.id])
        self.assertEqual(store.pointer(c.id), (0.2, 0.3))
        store.shift(c.id, -1)
        self.assertEqual([s.id for s in store.steps_for("A", 0)], [c.id, a.id])
        store.shift(c.id, -1)
        self.assertEqual(store.pointer(c.id), None)
        inserted = store.add("pause", "A", after=c.id)
        self.assertEqual([s.id for s in store.steps_for("A", 0)], [c.id, inserted.id, a.id])
        store.remove_all()
        self.assertTrue(store.is_empty)

    def test_atomic_failure_keeps_previous_file(self):
        store = self.store()
        store.add("speak", "A")
        previous = store.url.read_bytes()
        with patch("manimble_linux.script.os.replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.add("pause", "A")
        self.assertEqual(store.url.read_bytes(), previous)
        self.assertEqual(list(self.root.glob(".script-*")), [])

    def test_bad_data_never_silently_replaces_file(self):
        path = self.root / "script.json"
        path.write_text('{"version":99,"steps":[]}')
        with self.assertRaisesRegex(ValueError, "version"):
            self.store()
        self.assertIn('99', path.read_text())
        with self.assertRaises(ValueError):
            ScriptStep.from_dict({"id": "a", "scene": "A", "kind": "move", "speed": float("nan")})


class MotionTests(unittest.TestCase):
    def test_arc_length_aspect_duration_and_zero(self):
        motion = ScriptMotion([(0, 0), (0, 1), (1, 1)], (16, 9), 0.5)
        self.assertEqual(motion.length, 1 + 9 / 16)
        self.assertEqual(motion.duration, 3.125)
        self.assertEqual(ScriptMotion([(0, 0), (0.001, 0)], speed=2).duration, 0.2)
        self.assertEqual(ScriptMotion([(0, 0), (1, 0)], speed=0).duration, 50)
        self.assertEqual(ScriptMotion([(0.2, 0.3)]).duration, 0)
        self.assertEqual(ScriptMotion([]).point(2), (0, 0))

    def test_swift_easing_golden(self):
        motion = ScriptMotion([(0, 0), (0.5, 0)], (16, 9), 0.5)
        # Swift: u - sin(2πu)/(2π), evaluated independently at quarter times.
        self.assertAlmostEqual(motion.point(0.25)[0], 0.04542252845405233, places=14)
        self.assertEqual(motion.point(0.5), (0.25, 0))
        self.assertAlmostEqual(motion.point(0.75)[0], 0.4545774715459477, places=14)
        self.assertEqual(motion.point(-1), (0, 0))
        self.assertEqual(motion.point(2), (0.5, 0))

    def test_swift_wobble_golden_four_phases(self):
        motion = ScriptMotion([(0, 0), (0.5, 0)], (16, 9), 0.5, 1, "x")
        self.assertEqual(motion.hash, 4953295298048672641)
        for at, expected in [(0.25, (0.05147475134087874, 0.0006358499673543674)),
                             (0.5, (0.25378686999157657, -0.0027533781320133524)),
                             (0.75, (0.4461550267503999, 0.0013066593367475716))]:
            actual = motion.point(at)
            self.assertAlmostEqual(actual[0], expected[0], places=14)
            self.assertAlmostEqual(actual[1], expected[1], places=14)
        self.assertEqual(len(set(motion.phases)), 4)

    def test_hash_is_utf8_and_wraps_uint64(self):
        seed = "😀" * 200 + "a long overflowing seed"
        reference = 1469598103934665603
        for byte in seed.encode("utf-8"):
            reference = (reference ^ byte) * 1099511628211
            reference %= 2 ** 64
        self.assertEqual(ScriptMotion.seed_hash(seed), reference)
        self.assertLess(reference, 2 ** 64)
        self.assertEqual(ScriptMotion.seed_hash(""), 1469598103934665603)

    def test_polyline_arc_length_not_control_point_index(self):
        motion = ScriptMotion([(0, 0), (0.1, 0), (1, 0)], speed=1)
        self.assertAlmostEqual(motion.point(0.5)[0], 0.5)


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SchedulerTests(TempCase):
    def performer(self):
        clock = FakeClock()
        return ScriptPerformer(monotonic=clock, sleeper=lambda seconds: None), clock

    def test_pause_clock_freezes_every_interval(self):
        source = FakeClock()
        clock = ScriptClock(source)
        source.advance(2)
        self.assertEqual(clock.elapsed, 2)
        clock.paused = True
        source.advance(50)
        self.assertEqual(clock.elapsed, 2)
        clock.paused = False
        source.advance(0.25)
        self.assertEqual(clock.elapsed, 2.25)

    def test_wait_does_not_finish_while_paused(self):
        performer, clock = self.performer()
        wait = performer.wait(1)
        next(wait)
        clock.advance(0.4)
        next(wait)
        performer.paused = True
        clock.advance(100)
        next(wait)
        self.assertAlmostEqual(performer.clock.elapsed, 0.4)
        performer.paused = False
        clock.advance(0.59)
        next(wait)
        clock.advance(0.02)
        with self.assertRaises(StopIteration):
            next(wait)

    def test_move_clock_pause_and_navigation_interrupt(self):
        performer, clock = self.performer()
        move = performer.move(ScriptStep("motion", "A", kind="move", path=[[0.9, 0.5]]))
        next(move)
        self.assertEqual(performer.pointer, (0.5, 0.5))
        clock.advance(0.2)
        next(move)
        point = performer.pointer
        performer.paused = True
        clock.advance(10)
        next(move)
        self.assertEqual(performer.pointer, point)
        performer.paused = False
        clock.advance(0.61)
        with self.assertRaises(StopIteration):
            next(move)
        self.assertEqual(performer.pointer, (0.9, 0.5))
        interrupted = performer.move(ScriptStep("other", "A", kind="move", path=[[0.1, 0.1]]))
        next(interrupted)
        performer.interrupt = lambda: True
        with self.assertRaises(StopIteration):
            next(interrupted)
        self.assertEqual(performer.pointer, (0.9, 0.5))

    def test_runner_waits_for_animation_then_script_then_tail(self):
        clock = FakeClock()
        store = ScriptStore(self.root / "script.json")
        store.steps = [ScriptStep("pause", "A", kind="pause", seconds=1)]
        store.hold = 2

        class Show:
            page_index = 0
            page_count = 2
            script_page_ready = False
            ended = False

            @property
            def script_page(self):
                return ("A", 0) if self.page_index == 0 else ("B", 0)

            def next(self):
                self.page_index += 1

            def exit(self):
                self.ended = True

        show = Show()
        runner = ScriptRunner(store, show, monotonic=clock, sleeper=lambda seconds: None)
        hidden = []
        runner.performer.draw = hidden.append
        gen = runner.run()
        next(gen)
        clock.advance(10)  # Animation is not settled: none of this counts as script time.
        next(gen)
        self.assertEqual(show.page_index, 0)
        show.script_page_ready = True
        next(gen)
        clock.advance(1.01)
        next(gen)  # 0.4 s tail starts now.
        self.assertEqual(show.page_index, 0)
        runner.performer.paused = True
        clock.advance(50)
        next(gen)
        self.assertEqual(show.page_index, 0)
        runner.performer.paused = False
        clock.advance(0.41)
        next(gen)
        self.assertEqual(show.page_index, 1)
        clock.advance(2.01)
        next(gen)
        self.assertFalse(show.ended)
        clock.advance(0.41)
        with self.assertRaises(StopIteration):
            next(gen)
        self.assertTrue(show.ended)
        self.assertEqual(hidden, [None, None])


class FakeSecretService:
    def __init__(self):
        self.values = {}
        self.calls = []

    def get_password(self, service, account):
        self.calls.append(("get", service, account))
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.calls.append(("set", service, account))
        self.values[service, account] = value

    def delete_password(self, service, account):
        self.calls.append(("delete", service, account))
        self.values.pop((service, account), None)


def wav_bytes(seconds=0.1, rate=24000):
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * round(seconds * rate))
    return data.getvalue()


@contextmanager
def provider_fixture():
    rows = []
    state = {"status": 200, "delay": 0}
    audio = wav_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            rows.append((self.command, self.path, dict(self.headers), None))
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(json.dumps({"voices": [{"voice_id": "v2", "name": "Zoe"},
                                                      {"voice_id": "v1", "name": "Amy"}], "data": []}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rows.append((self.command, self.path, dict(self.headers), body))
            time.sleep(state["delay"])
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(audio if state["status"] == 200 else b"fixture refused")

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", rows, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CloudTests(TempCase):
    def setUp(self):
        super().setUp()
        self.backend = FakeSecretService()
        self.mock_backend = patch("manimble_linux.voice.secret_backend", return_value=self.backend)
        self.mock_backend.start()
        self.addCleanup(self.mock_backend.stop)
        for service in CloudVoiceService:
            service.set_key("fixture-secret-" + service.value)

    def test_exact_elevenlabs_payload_and_cache_hash(self):
        with provider_fixture() as (url, rows, state):
            voice = CloudVoice("cloud:elevenlabs:a/b", cache_dir=self.root, base_url=url)
            path = voice.audio("héllo", rate=0.7)
            expected = hashlib.sha256("cloud:elevenlabs:a/b|1.200|héllo".encode()).hexdigest() + ".mp3"
            self.assertEqual(path.name, expected)
            self.assertEqual(rows[0][1], "/v1/text-to-speech/a%2Fb?output_format=mp3_44100_128")
            self.assertEqual(rows[0][3], {"text": "héllo", "model_id": "eleven_multilingual_v2", "voice_settings": {"speed": 1.2}})
            self.assertEqual(rows[0][2]["Xi-Api-Key"], "fixture-secret-elevenlabs")
            self.assertEqual(voice.speed(0), 0.7)
            with patch("manimble_linux.voice.secret_backend", side_effect=VoiceError("locked")):
                self.assertEqual(voice.audio("héllo", rate=0.7), path)
            self.assertEqual(len(rows), 1)
            self.assertNotIn(b"fixture-secret", path.read_bytes())

    def test_exact_openai_payload_and_clamps(self):
        with provider_fixture() as (url, rows, state):
            voice = CloudVoice("cloud:openai:alloy", cache_dir=self.root, base_url=url)
            voice.audio("Hello", rate=3)
            self.assertEqual(rows[0][1], "/v1/audio/speech")
            self.assertEqual(rows[0][2]["Authorization"], "Bearer fixture-secret-openai")
            self.assertEqual(rows[0][3], {"model": "tts-1-hd", "voice": "alloy", "input": "Hello", "speed": 4.0, "response_format": "mp3"})
            self.assertEqual(voice.speed(0), 0.25)

    def test_voice_lists_verification_and_secret_accounts(self):
        with provider_fixture() as (url, rows, state):
            self.assertEqual(CloudVoiceService.elevenlabs.refresh_voices(base_url=url), [("v1", "Amy"), ("v2", "Zoe")])
            CloudVoiceService.openai.verify("verify-only", base_url=url)
            self.assertEqual(rows[1][1], "/v1/models")
            self.assertEqual(rows[1][2]["Authorization"], "Bearer verify-only")
            self.assertTrue(all(row[1] == "org.pachterlab.Manimble.voice" for row in self.backend.calls))
            self.assertEqual({row[2] for row in self.backend.calls}, {"elevenlabs", "openai"})
            CloudVoiceService.openai.set_key(None)
            self.assertFalse(CloudVoiceService.openai.connected)

    def test_http_and_keyring_failures_do_not_create_cache_or_plaintext(self):
        with provider_fixture() as (url, rows, state):
            voice = CloudVoice("cloud:openai:alloy", cache_dir=self.root, base_url=url)
            state["status"] = 401
            with self.assertRaisesRegex(VoiceError, "refused.*401"):
                voice.audio("failure")
            self.assertEqual(list(self.root.iterdir()), [])
            with patch("manimble_linux.voice.secret_backend", side_effect=VoiceError("Secret Service locked")):
                with self.assertRaisesRegex(VoiceError, "locked"):
                    voice.audio("uncached")
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual(len(rows), 1)

    def test_in_flight_deduplicates_and_failure_can_retry(self):
        with provider_fixture() as (url, rows, state):
            voice = CloudVoice("cloud:elevenlabs:v1", cache_dir=self.root, base_url=url)
            state["delay"] = 0.1
            with ThreadPoolExecutor(max_workers=4) as pool:
                answers = list(pool.map(lambda _: voice.audio("same words"), range(4)))
            self.assertEqual(len(set(answers)), 1)
            self.assertEqual(len(rows), 1)
            state["status"] = 500
            with self.assertRaisesRegex(VoiceError, "500"):
                voice.audio("retry words")
            state["status"] = 200
            self.assertTrue(voice.audio("retry words").is_file())
            self.assertEqual(len(rows), 3)


class MoviePlanTests(TempCase):
    def test_prepare_once_holds_letterbox_and_event_timing(self):
        store = ScriptStore(self.root / "script.json")
        store.steps = [ScriptStep("speech", "A", kind="speak", text="Hello"),
                       ScriptStep("move", "A", kind="move", path=[[0.8, 0.5]], speed=1),
                       ScriptStep("pause", "A", build=1, kind="pause", seconds=0.6),
                       ScriptStep("move2", "B", kind="move", path=[[0.2, 0.5]], speed=1)]
        calls = []

        def speech(text, voice, rate):
            calls.append((text, voice, rate))
            return self.root / "speech.wav", 0.5

        export = ScriptMovieExport.prepare_from_audio(store, ["A", "B"], (1280, 720), 4 / 3, 30,
                                                       self.root / "movie.mp4", speech)
        self.addCleanup(export.discard)
        self.assertEqual(calls, [("Hello", None, 0.5)])
        self.assertEqual(export.slide, (160.0, 0.0, 960.0, 720.0))
        self.assertAlmostEqual(export.holds["A"]["0"], 1.2)
        self.assertAlmostEqual(export.holds["A"]["1"], 1)
        self.assertEqual(export.unscripted, 3.4)
        self.assertEqual(export.builds["B"][0].events[0]["motion"]["points"][0], (0.8, 0.5))
        segments = [{"scene": "A", "build": 0, "start": 0, "end": 2.2, "hold": 1.2},
                    {"scene": "A", "build": 1, "start": 2.2, "end": 3.2, "hold": 1},
                    {"scene": "B", "build": 0, "start": 3.2, "end": 5, "hold": 1}]
        moves, audio = timeline(export.job(segments))
        self.assertAlmostEqual(audio[0][0], 1)
        pointer = PointerTimeline(moves)
        self.assertIsNone(pointer.at(1.49))
        self.assertAlmostEqual(pointer.at(1.500001)[0], 0.5, places=10)
        self.assertAlmostEqual(pointer.at(1.500001)[1], 0.5, places=10)
        self.assertEqual(pointer.at(2.3), (0.8, 0.5))
        self.assertIsNone(pointer.at(3.3))
        self.assertEqual(pointer.at(4), (0.8, 0.5))

    def test_incremental_audio_resampling_and_silence(self):
        import numpy as np
        path = self.root / "speech.wav"
        path.write_bytes(wav_bytes(0.1, rate=24000))
        blocks = list(audio_blocks([(0.1, str(path), 0.1)], 14400))
        self.assertEqual(sum(b.shape[1] for b in blocks), 14400)
        self.assertLessEqual(max(b.shape[1] for b in blocks), 1024)
        self.assertTrue(all(b.dtype == np.float32 for b in blocks))
        self.assertTrue(all(np.all(b == 0) for b in blocks))

    def test_failed_preparation_retains_existing_destination(self):
        destination = self.root / "movie.mp4"
        destination.write_bytes(b"previous successful movie")
        store = ScriptStore(self.root / "script.json")
        store.steps = [ScriptStep("speech", "A", kind="speak", text="Hello")]

        def speech(*args):
            raise VoiceError("synthesis failed")

        with self.assertRaisesRegex(VoiceError, "synthesis failed"):
            ScriptMovieExport.prepare_from_audio(store, ["A"], (1280, 720), 16 / 9, 30, destination, speech)
        self.assertEqual(destination.read_bytes(), b"previous successful movie")
        self.assertEqual(list(self.root.glob(".manimble-script-*")), [])

class NativeTests(TempCase):
    @unittest.skipUnless(shutil.which("espeak-ng"), "espeak-ng is not installed; native synthesis requires that package")
    def test_native_wav_shared_by_live_and_export_and_foreign_id_warning(self):
        native = NativeVoice(self.root)
        first, warning = native.audio("A native narration test.")
        again, _ = native.audio("A native narration test.")
        self.assertEqual(first, again)
        self.assertEqual(first.read_bytes()[:4], b"RIFF")
        foreign, warning = native.audio("A native narration test.", "com.apple.voice.foreign")
        self.assertEqual(first, foreign)
        self.assertIn("saved identifier is unchanged", warning)


if __name__ == "__main__":
    unittest.main()
