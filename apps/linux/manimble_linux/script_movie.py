"""Narrated movie preparation and cancellable incremental FFmpeg/PyAV composition.

The engine still owns clip rendering and silent letterboxing. This process reads
that result sequentially, adds only the script's pointer/audio, and returns a
staged file. Only the GUI publishes it, so a killed/stale worker cannot replace a
previous successful export.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace

from .script import ScriptMotion, ScriptPointer


def _snapshot(store):
    return SimpleNamespace(steps=deepcopy(store.steps), voice=store.voice, rate=store.rate, hold=store.hold)


@dataclass
class Build:
    duration: float
    events: list[dict] = field(default_factory=list)


class ScriptMovieExport:
    gap = 0.4

    def __init__(self, destination, frame, aspect, fps, hold):
        self.destination = Path(destination).absolute()
        self.frame = tuple(frame)
        self.fps = int(fps)
        self.unscripted = hold + self.gap
        w = min(frame[0], frame[1] * aspect)
        h = w / aspect
        self.slide = ((frame[0] - w) / 2, (frame[1] - h) / 2, w, h)
        self.work = Path(tempfile.mkdtemp(prefix=".manimble-script-", dir=self.destination.parent))
        self.builds: dict[str, dict[int, Build]] = {}
        self.worker = None
        self.cancelled = False
        self.generation = 0

    @property
    def base(self):
        return self.work / "show.mp4"

    @property
    def holds(self):
        return {scene: {str(index): build.duration + self.gap for index, build in per.items()}
                for scene, per in self.builds.items()}

    @staticmethod
    def _move(step, pointer, size):
        args = {"points": [pointer or (0.5, 0.5)] + step.points, "size": size,
                "speed": step.speed, "noise": step.noise, "seed": step.id}
        return ScriptMotion(**args), args

    def _prepare_builds(self, store, scenes):
        """Yield only speech requests; both callers share the identical timeline."""
        pointer = None
        for scene in scenes:
            mine = [s for s in store.steps if s.scene == scene]
            for build in sorted({s.build for s in mine}):
                t, events = 0.0, []
                for step in (s for s in mine if s.build == build):
                    if self.cancelled:
                        raise RuntimeError("Movie export cancelled")
                    if step.kind == "pause":
                        t += step.seconds
                    elif step.kind == "speak" and step.text.strip():
                        speech = yield step
                        if speech:
                            path, duration = speech
                            if duration > 0:
                                events.append({"kind": "audio", "at": t, "url": str(path), "length": duration})
                                t += duration
                    elif step.kind == "move" and step.points:
                        motion, args = self._move(step, pointer, self.slide[2:])
                        events.append({"kind": "move", "at": t, "motion": args})
                        t += motion.duration
                        pointer = step.points[-1]
                self.builds.setdefault(scene, {})[build] = Build(t, events)
        if self.cancelled:
            raise RuntimeError("Movie export cancelled")

    @classmethod
    def prepare(cls, store, scenes, frame, aspect, fps, destination, voices, progress=lambda i, n: None,
                on_created=lambda export: None):
        from .workers import run_blocking
        store = _snapshot(store)
        export = yield run_blocking(cls, destination, frame, aspect, fps, store.hold)
        export.voices = voices
        on_created(export)
        try:
            total = sum(s.kind == "speak" and bool(s.text.strip()) for scene in scenes for s in store.steps if s.scene == scene)
            spoken = 0
            timeline = export._prepare_builds(store, scenes)
            audio = None
            while True:
                try:
                    step = timeline.send(audio)
                except StopIteration:
                    break
                spoken += 1
                progress(spoken, total)
                audio = yield from voices.speech(step.text, store.voice, store.rate)
            return export
        except GeneratorExit:
            run_blocking(export.discard)
            raise
        except Exception:
            yield run_blocking(export.discard)
            raise

    @classmethod
    def prepare_from_audio(cls, store, scenes, frame, aspect, fps, destination, speech):
        """Blocking preparation for callers already on a worker.

        ``speech(text, voice, rate)`` supplies the same audio used for playback.
        """
        store = _snapshot(store)
        export = cls(destination, frame, aspect, fps, store.hold)
        try:
            timeline = export._prepare_builds(store, scenes)
            audio = None
            while True:
                try:
                    step = timeline.send(audio)
                except StopIteration:
                    break
                audio = speech(step.text, store.voice, store.rate)
            return export
        except BaseException:
            export.discard()
            raise

    def job(self, segments):
        return {"base": str(self.base), "output": str(self.work / "scripted.mp4"),
                "fps": self.fps, "slide": self.slide, "segments": segments,
                "builds": {scene: {str(k): {"duration": b.duration, "events": b.events} for k, b in per.items()}
                           for scene, per in self.builds.items()}}

    def finish(self, segments, python, env=None):
        from .workers import WorkerProcess, run_blocking
        if self.cancelled:
            raise RuntimeError("Movie export cancelled")
        generation = self.generation
        self.worker = WorkerProcess(python, "script_movie", self.job(segments), env)
        if getattr(self, "on_progress", None):
            self.worker.progress.connect(self.on_progress)
        try:
            result = yield self.worker.start()
            if self.cancelled or generation != self.generation:
                raise RuntimeError("Movie export cancelled")
            # A single same-filesystem atomic operation, after the generation check.
            os.replace(result["path"], self.destination)
            return {"path": str(self.destination), "duration": result["duration"], "frames": result["frames"]}
        finally:
            self.worker = None
            run_blocking(self.discard)

    def cancel(self):
        self.cancelled = True
        self.generation += 1
        if hasattr(self, "voices"):
            self.voices.cancel()
        if self.worker:
            self.worker.cancel()
        else:
            from .workers import run_blocking
            run_blocking(self.discard)

    def discard(self):
        shutil.rmtree(self.work, ignore_errors=True)


def timeline(job):
    """Time-stamped actions, ordered as the engine's segments (including hides)."""
    moves, audio = [], []
    last_scene = None
    for segment in job["segments"]:
        scene = segment["scene"]
        if scene != last_scene:
            moves.append((float(segment["start"]), "hide", None))
            last_scene = scene
        build = job["builds"].get(scene, {}).get(str(segment["build"]))
        if not build:
            continue
        start = max(float(segment["start"]), float(segment["end"]) - float(segment["hold"]))
        for event in build["events"]:
            at = start + event["at"]
            if event["kind"] == "move":
                moves.append((at, "move", ScriptMotion(**event["motion"])))
            elif event["kind"] == "audio":
                audio.append((at, event["url"], event["length"]))
    return sorted(moves, key=lambda row: row[0]), sorted(audio, key=lambda row: row[0])


class PointerTimeline:
    def __init__(self, actions):
        self.actions, self.index = actions, 0
        self.point = None
        self.motion = None
        self.start = 0.0

    def at(self, seconds):
        while self.index < len(self.actions) and self.actions[self.index][0] <= seconds:
            self.start, kind, motion = self.actions[self.index]
            if kind == "hide":
                self.point, self.motion = None, None
            else:
                self.motion = motion
                self.point = motion.points[0]
            self.index += 1
        if self.motion:
            elapsed = seconds - self.start
            self.point = self.motion.point(elapsed) if elapsed < self.motion.duration else self.motion.points[-1]
        return self.point


def audio_blocks(events, total_samples, block_size=1024):
    """Yield mono float32 PCM, resampling each utterance with bounded buffers."""
    import av
    import numpy as np
    cursor = 0
    silence = np.zeros((1, block_size), dtype=np.float32)

    def zeros(count):
        while count > 0:
            n = min(count, block_size)
            yield silence[:, :n]
            count -= n

    for at, path, length in events:
        start = max(cursor, round(at * 48000))
        if start >= total_samples:
            break
        for block in zeros(start - cursor):
            yield block
            cursor += block.shape[1]
        remaining = min(round(length * 48000), total_samples - cursor)
        with av.open(path) as source:
            stream = next(iter(source.streams.audio), None)
            if stream is None:
                raise ValueError(f"Narration file has no audio stream: {path}")
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=48000)

            def frames():
                for frame in source.decode(stream):
                    yield from resampler.resample(frame)
                yield from resampler.resample(None)

            for frame in frames():
                values = frame.to_ndarray()
                for offset in range(0, min(frame.samples, remaining), block_size):
                    n = min(block_size, remaining, frame.samples - offset)
                    if n <= 0:
                        break
                    block = values[:, offset:offset + n]
                    yield block
                    cursor += n
                    remaining -= n
                if remaining <= 0:
                    break
        # Decoders may round a source duration by a sample; preserve the timeline.
        for block in zeros(remaining):
            yield block
            cursor += block.shape[1]
    for block in zeros(total_samples - cursor):
        yield block


def compose(job, progress=lambda fraction, text="": None):
    """Worker entry point. No entire movie or audio track is held in memory."""
    import av
    from PIL import ImageDraw

    output = Path(job["output"])
    fps = int(job["fps"])
    moves, speech = timeline(job)
    pointer = PointerTimeline(moves)
    total = max((float(s["end"]) for s in job["segments"]), default=0)
    if total <= 0:
        raise ValueError("The engine returned no movie segments")
    total_samples = round(total * 48000)
    frame_index = 0
    try:
        with av.open(job["base"]) as source, av.open(str(output), mode="w", options={"movflags": "+faststart"}) as destination:
            video = next(iter(source.streams.video), None)
            if video is None:
                raise ValueError("The encoded movie has no video stream")
            out_video = destination.add_stream("libx264", rate=fps)
            out_video.width, out_video.height = video.width, video.height
            out_video.pix_fmt = "yuv420p"
            out_video.options = {"crf": "18", "preset": "medium"}
            out_video.time_base = Fraction(1, fps)
            out_audio = destination.add_stream("aac", rate=48000) if speech else None
            if out_audio:
                out_audio.layout = "mono"
                out_audio.bit_rate = 192000
                out_audio.time_base = Fraction(1, 48000)
            blocks = iter(audio_blocks(speech, total_samples)) if speech else iter(())
            audio_cursor = 0
            pending = next(blocks, None)
            for frame in source.decode(video):
                seconds = frame_index / fps
                image = frame.to_image()
                point = pointer.at(seconds)
                if point is not None:
                    draw = ImageDraw.Draw(image)
                    polygon = ScriptPointer.polygon(point, job["slide"])
                    line_width = max(1, round(1.1 * max(14, job["slide"][3] * 0.045) / 20))
                    draw.polygon(polygon, fill="white")
                    draw.line(polygon + [polygon[0]], fill="black", width=line_width, joint="curve")
                rendered = av.VideoFrame.from_image(image)
                rendered.pts, rendered.time_base = frame_index, Fraction(1, fps)
                for packet in out_video.encode(rendered):
                    destination.mux(packet)
                target = min(total_samples, round((frame_index + 1) * 48000 / fps))
                while pending is not None and audio_cursor < target:
                    chunk = av.AudioFrame.from_ndarray(pending, format="fltp", layout="mono")
                    chunk.sample_rate, chunk.pts, chunk.time_base = 48000, audio_cursor, Fraction(1, 48000)
                    audio_cursor += chunk.samples
                    for packet in out_audio.encode(chunk):
                        destination.mux(packet)
                    pending = next(blocks, None)
                frame_index += 1
                if frame_index % max(1, fps // 2) == 0:
                    progress(min(0.99, seconds / total), f"Compositing {seconds:.1f}/{total:.1f} s")
            if not frame_index:
                raise ValueError("The encoded movie has no decoded frames")
            for packet in out_video.encode(None):
                destination.mux(packet)
            while pending is not None:
                chunk = av.AudioFrame.from_ndarray(pending, format="fltp", layout="mono")
                chunk.sample_rate, chunk.pts, chunk.time_base = 48000, audio_cursor, Fraction(1, 48000)
                audio_cursor += chunk.samples
                for packet in out_audio.encode(chunk):
                    destination.mux(packet)
                pending = next(blocks, None)
            if out_audio:
                for packet in out_audio.encode(None):
                    destination.mux(packet)
        with output.open("rb") as file:
            os.fsync(file.fileno())
        progress(1, "Narrated movie ready")
        return {"path": str(output), "duration": frame_index / fps, "frames": frame_index}
    except BaseException:
        output.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(compose)
