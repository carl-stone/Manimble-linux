"""Disk-backed working copies. Mirrored asset links are convenience, not a sandbox."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid

GENERATED = frozenset({"slides", "media", "__pycache__", ".manimble"})
AGENT_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
AGENT_TEXT = '''# Editing this deck from Manimble

This folder is the working copy of a Manim Slides deck that is open in Manimble, a slide editor for Linux.
The user sees the slides in Manimble while you edit the code here. (This file was written by Manimble and is
not part of the deck; don't edit it.)

## How edits reach the slides

- Edit the deck's files **in this folder** (`$MANIMBLE_DECK`). Manimble notices saved changes within a few
  seconds and re-renders the slides they affect, so the user sees each edit almost at once.
- Never write to `$MANIMBLE_ORIGINAL_DECK`. That is the user's real deck. The user writes the working copy
  back to it with File ▸ Save in Manimble, or throws the changes away with File ▸ Revert to Original.
- Don't run `manim` or `manim-slides` yourself. Manimble renders. If you need to check that a file parses,
  `python -m py_compile <file>` is enough.
- Non-code subfolders here (and the `assets` folder next to this one) are links to the original project, so
  writing in them changes the user's files directly, with no Save or Revert. Authored symlinks have the same
  limitation. This is not a sandbox. Prefer files at the top level of this folder or its copied Python packages.
- This folder isn't a git repository. Run `diff -ru "$MANIMBLE_ORIGINAL_DECK" .` to see what has changed.

## How the deck is put together

- Each slide is a `Slide` subclass (manim-slides) in one of the `.py` files here. Its `construct` builds it.
- `self.next_slide()` separates the builds of a slide. The presenter clicks once for each build.
- `order.txt`, if present, lists the slides' class names in show order, separated by spaces. A new slide must be
  added there to appear in the show.
- A class line ending in `# manimble hidden` is a hidden slide: it stays in the deck but is skipped in the show.
- Only `self.play(...)` and `self.wait(...)` record frames. An object added with `self.add(...)` after a
  build's last `play` or `wait` appears one click late, so put adds before them.
- Code runs with this folder as the working directory. Refer to a picture next to the slide files as
  `str(__import__("pathlib").Path(__file__).resolve().parent / "name.png")`, the way Manimble does.

## Keep the slides editable in Manimble

The user also edits these slides by hand: dragging, resizing, retyping text and recolouring. Manimble does it by
rewriting the code that made each object, so write code it can follow:

- Make each object with one expression that has literal arguments, e.g.
  `title = Text("Results", font_size=48).move_to((0, 3, 0))`.
- Give positions as numbers in `.move_to((x, y, 0))` rather than computing them, when the object should be
  movable. The frame is normally about 14.2 units wide and 8 high, centred on the origin.
- Use loops, list comprehensions and helper functions only for things the user won't want to move or retype
  one at a time.
- A helper function that makes `Text` must accept and pass on `**kwargs`. Manimble stores bold, italic and
  colour on part of a text as `t2w`, `t2s` and `t2c` arguments.
- Don't rename slide classes or reformat code you weren't asked to change. Manimble finds objects by the source
  line that made them, and the user's own edits are in that code too.
- Leave alone the helper functions Manimble adds at the end of a file (such as `underline_chars`), and the
  `manimble_decoration` attributes they set.

## Working with the user

- Make one change at a time and say which slide it's on, so the user can look at it before the next one.
- If a slide fails to render, Manimble shows the error on that slide. Ask the user for the message if you need
  it, then fix the code.
'''


class WorkspaceError(OSError):
    pass


class SaveConflict(WorkspaceError):
    def __init__(self, files):
        self.files = files
        super().__init__("The original changed on disk: " + ", ".join(files))


def excluded(name: str) -> bool:
    return (name.startswith(".") or name in GENERATED or name.endswith((".manimble.tmp", ".tmp", ".swp", ".pyc", "~")))


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".manimble-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        with open(temporary, "rb") as f:
            os.fsync(f.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def same_file(a: Path, b: Path) -> bool:
    try:
        if a.is_symlink() or b.is_symlink() or not a.is_file() or not b.is_file():
            return False
        if a.stat().st_size != b.stat().st_size:
            return False
        with a.open("rb") as left, b.open("rb") as right:
            while True:
                chunk = left.read(1024 * 1024)
                if chunk != right.read(1024 * 1024):
                    return False
                if not chunk:
                    return True
    except FileNotFoundError:
        return False


def _holds_python(folder: Path) -> bool:
    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not excluded(d) and not (Path(root) / d).is_symlink()]
        if any(n.endswith(".py") for n in files):
            return True
    return False


class Workspace:
    @classmethod
    def open(cls, deck, paths=None):
        return cls(deck, paths)

    def __init__(self, deck, paths=None):
        if paths is None:
            from .paths import AppPaths
            paths = AppPaths.from_environment()
        self.original = Path(deck).expanduser().resolve(strict=True)
        if not self.original.is_dir():
            raise WorkspaceError(f"Not a deck folder: {deck}")
        self.paths = paths
        parent = self.original.parent
        self.project_name = parent.name + "-" + self.original.name
        self.deck_relative_path = parent.name + "/" + self.original.name
        digest = hashlib.sha256(os.fsencode(self.original)).hexdigest()[:12]
        self.root = paths.work / f"{self.original.name}-{digest}-{uuid.uuid4()}"
        self.work = self.root / parent.name / self.original.name
        self.baseline_dir = self.root / ".baseline"
        self.own_files = set()
        self._baseline = set()
        self._compared = {}
        try:
            self.work.mkdir(parents=True)
            self.baseline_dir.mkdir()
            for source, target, skip in ((parent.parent, self.root, parent.name), (parent, self.work.parent, self.original.name)):
                for child in source.iterdir():
                    if child.name != skip and not child.name.startswith("."):
                        (target / child.name).symlink_to(child, target_is_directory=child.is_dir())
            for child in self.original.iterdir():
                if child.name.startswith(".") or child.name == "__pycache__":
                    continue
                target = self.work / child.name
                if child.is_symlink():
                    target.symlink_to(os.readlink(child), target_is_directory=child.is_dir())
                elif child.is_dir():
                    if child.name == "slides" or (child.name not in GENERATED and _holds_python(child)):
                        shutil.copytree(child, target, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
                    else:
                        target.symlink_to(child, target_is_directory=True)
                else:
                    shutil.copy2(child, target)
            for rel in self.deck_files():
                atomic_copy(self.work / rel, self.baseline_dir / rel)
                self._baseline.add(rel)
            for name in AGENT_FILES:
                if not os.path.lexists(self.work / name):
                    (self.work / name).write_text(AGENT_TEXT, encoding="utf-8")
                    self.own_files.add(name)
        except BaseException:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    @staticmethod
    def cache_dir(deck, paths=None):
        if paths is None:
            from .paths import AppPaths
            paths = AppPaths.from_environment()
        original = Path(deck).resolve()
        digest = hashlib.sha256(os.fsencode(original)).hexdigest()[:12]
        target = paths.render_cache / f"{original.name}-{digest}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def deck_files(self):
        result = []
        for root, dirs, files in os.walk(self.work, followlinks=False):
            base = Path(root)
            dirs[:] = [n for n in dirs if not excluded(n) and not (base / n).is_symlink()]
            for name in files:
                path = base / name
                rel = path.relative_to(self.work).as_posix()
                if not excluded(name) and rel not in self.own_files and not path.is_symlink() and path.is_file():
                    result.append(rel)
        return sorted(result)

    def changed_files(self):
        present = set(self.deck_files())
        changed = set(self._baseline - present)
        for rel in present:
            path = self.work / rel
            st = path.stat()
            signature = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            cached = self._compared.get(rel)
            if cached is None or cached[0] != signature:
                cached = (signature, rel in self._baseline and same_file(path, self.baseline_dir / rel))
                self._compared[rel] = cached
            if not cached[1]:
                changed.add(rel)
        return sorted(changed)

    @property
    def is_modified(self):
        return bool(self.changed_files())

    @property
    def original_missing(self):
        return not self.original.is_dir()

    def conflict_files(self):
        return [rel for rel in self.changed_files()
                if (not same_file(self.original / rel, self.baseline_dir / rel) if rel in self._baseline
                    else os.path.lexists(self.original / rel))]

    @staticmethod
    def _check_parents(base, relative):
        path = base
        for component in Path(relative).parts[:-1]:
            path /= component
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise WorkspaceError(f"Refusing to write through changed directory: {path}")

    def save(self, overwrite_conflicts=False):
        if self.original_missing:
            raise WorkspaceError(f"The original deck is no longer at {self.original}. Your edits remain in the working copy. Use Save Project Copy to preserve them.")
        conflicts = self.conflict_files()
        if conflicts and not overwrite_conflicts:
            raise SaveConflict(conflicts)
        for rel in self.changed_files():
            self._check_parents(self.original, rel)
            source, destination = self.work / rel, self.original / rel
            if source.is_file() and not source.is_symlink():
                atomic_copy(source, destination)
                atomic_copy(source, self.baseline_dir / rel)
                self._baseline.add(rel)
            else:
                if destination.is_dir() and not destination.is_symlink():
                    raise WorkspaceError(f"A directory replaced the deleted file: {destination}")
                destination.unlink(missing_ok=True)
                (self.baseline_dir / rel).unlink(missing_ok=True)
                self._baseline.discard(rel)
            self._compared.pop(rel, None)

    def revert(self):
        for rel in self.changed_files():
            self._check_parents(self.work, rel)
            path = self.work / rel
            if rel in self._baseline:
                atomic_copy(self.baseline_dir / rel, path)
            else:
                path.unlink(missing_ok=True)
            self._compared.pop(rel, None)

    def discard(self):
        shutil.rmtree(self.root)
