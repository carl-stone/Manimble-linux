"""Qt-independent portable project creation, opening, new-deck files and recents."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile

from .workspace import atomic_copy

MANIFEST_NAME = "manimble-project.json"
EXCLUDED = frozenset({"snap", "__pycache__", ".manimble", "venv", "env", "models", "node_modules", "cache", "caches"})


class PortableProjectError(ValueError):
    pass


def _excluded(name):
    return name.startswith(".") or name in EXCLUDED or name.endswith((".tmp", ".pyc", ".swp", "~", ".manimble.tmp"))


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PortableProjectError("Invalid project-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")) or re.match(r"^[A-Za-z]:", value):
        raise PortableProjectError(f"Unsafe project path: {value}")
    return path


def deck_url(project):
    project = Path(project)
    manifest_path = project / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "manimble-project" or manifest.get("version") != 1:
        raise PortableProjectError("Unsupported or invalid Manimble project manifest")
    relative = _relative(manifest.get("deckPath"))
    deck = (project / relative).resolve()
    if not deck.is_relative_to(project.resolve()) or deck == project.resolve() or not deck.is_dir():
        raise PortableProjectError("The deck folder listed in this project is missing or outside the project")
    return deck


def resolve_project(path):
    path = Path(path).expanduser().resolve(strict=True)
    if path.is_dir():
        return deck_url(path) or path
    if path.suffix.lower() == ".py" or (path.parent / "order.txt").is_file():
        return path.parent
    raise PortableProjectError("Choose a deck folder, Python file, project folder or .manimble archive")


def unpack_archive(archive, target, runtime):
    """The standalone CLI owns ZIP validation and staged extraction, including source-only decks."""
    target = Path(target).absolute()
    if os.path.lexists(target):
        raise FileExistsError(target)
    command = runtime.manimbler_command("--extract", str(Path(archive).absolute()), "--output", str(target))
    result = subprocess.run(command, env=runtime.child_env(), capture_output=True, text=True)
    if result.returncode:
        raise PortableProjectError(result.stderr.strip() or result.stdout.strip() or f"Extraction failed ({result.returncode})")
    deck = deck_url(target)
    if deck is None:
        raise PortableProjectError("The extracted project has no manifest")
    return deck


class PortableProject:
    manifest_name = MANIFEST_NAME
    deck_url = staticmethod(deck_url)
    unpack_archive = staticmethod(unpack_archive)

    @staticmethod
    def _roots(workspace):
        return (workspace.root.absolute(), workspace.original.parent.parent.resolve())

    @staticmethod
    def _source(workspace, raw):
        path = Path(os.path.abspath(os.path.expanduser(os.fspath(raw))))
        roots = PortableProject._roots(workspace)
        root = next((r for r in roots if path.is_relative_to(r)), None)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PortableProjectError(f"Missing, unreadable or looping dependency: {path}") from exc
        if root is None or not any(resolved.is_relative_to(r.resolve()) for r in roots):
            raise PortableProjectError(f"Dependency outside project roots: {path}\nAllowed roots:\n" + "\n".join(map(str, roots)))
        rel = path.relative_to(root)
        # Prefer the working version when a dependency was recorded through an original absolute path.
        working = workspace.root / rel
        if root != workspace.root and working.exists() and not working.is_symlink():
            path = working
        if not path.is_file() or not os.access(path, os.R_OK):
            raise PortableProjectError(f"Unavailable dependency: {path}")
        return path, rel

    @staticmethod
    def _copy_node(source, target, roots, *, ancestry=frozenset(), depth=0):
        if depth >= 64:
            raise PortableProjectError(f"Directory link depth exceeds 64: {source}")
        try:
            actual = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PortableProjectError(f"Broken or looping project link: {source}") from exc
        if not any(actual.is_relative_to(root.resolve()) for root in roots):
            raise PortableProjectError(f"Project link points outside allowed roots: {source} → {actual}")
        if actual.is_dir():
            stat = actual.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in ancestry:
                raise PortableProjectError(f"Directory link loop: {source}")
            target.mkdir(parents=True, exist_ok=True)
            for child in sorted(actual.iterdir()):
                if not _excluded(child.name):
                    PortableProject._copy_node(child, target / child.name, roots, ancestry=ancestry | {identity}, depth=depth + 1)
        elif actual.is_file():
            if not target.exists():
                atomic_copy(actual, target)
        else:
            raise PortableProjectError(f"Unreadable project asset: {source}")

    @staticmethod
    def _materialize(workspace, project):
        deck = project / workspace.deck_relative_path
        source_deck = workspace.work
        for manifest in sorted((deck / "slides").glob("*.json")):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            copied = {}
            for build in data.get("slides", []):
                materialized = False
                for key in ("file", "rev_file"):
                    raw = build.get(key)
                    if not raw:
                        continue
                    source = Path(raw)
                    if not source.is_absolute():
                        source = source_deck / source
                    try:
                        source = source.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise PortableProjectError(f"Missing presentation media: {raw} ({manifest.name})") from exc
                    if not source.is_file():
                        raise PortableProjectError(f"Missing presentation media: {source}")
                    if source not in copied:
                        digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:20]
                        relative = Path("slides") / "files" / manifest.stem / (digest + source.suffix)
                        atomic_copy(source, deck / relative)
                        copied[source] = relative.as_posix()
                    build[key] = copied[source]
                    materialized = True
                if materialized:
                    build["src"] = None
            manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _build(workspace, dependencies, project):
        roots = PortableProject._roots(workspace)
        deck = project / workspace.deck_relative_path
        deck.mkdir(parents=True)
        for child in sorted(workspace.work.iterdir()):
            if not _excluded(child.name) and child.name not in workspace.own_files:
                PortableProject._copy_node(child, deck / child.name, roots)
        assets = workspace.work.parent / "assets"
        if os.path.lexists(assets):
            PortableProject._copy_node(assets, deck.parent / "assets", roots)
        included = set()
        for raw in sorted(set(dependencies)):
            source, relative = PortableProject._source(workspace, raw)
            if any(_excluded(p) for p in relative.parts):
                continue
            if relative.is_relative_to(Path(workspace.deck_relative_path)) and relative.relative_to(workspace.deck_relative_path).as_posix() in workspace.own_files:
                continue
            included.add(relative.as_posix())
            target = project / relative
            if not target.exists():
                atomic_copy(source, target)
        PortableProject._materialize(workspace, project)
        manifest = {"format": "manimble-project", "version": 1, "projectName": workspace.project_name,
                    "deckPath": workspace.deck_relative_path, "dependencies": sorted(included),
                    "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        (project / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @staticmethod
    def _check_destination(workspace, dependencies, destination):
        resolved = destination.resolve()
        inputs = [workspace.root, workspace.original, workspace.original.parent / "assets", workspace.work.parent / "assets"]
        inputs.extend(Path(p) for p in dependencies)
        for source in inputs:
            source = source.resolve()
            if resolved == source or resolved.is_relative_to(source):
                raise PortableProjectError(f"Destination is inside copied input: {source}")

    @staticmethod
    def create_folder(workspace, dependencies, destination):
        destination = Path(destination).absolute()
        dependencies = tuple(dependencies)
        PortableProject._check_destination(workspace, dependencies, destination)
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".manimble-project-", dir=destination.parent))
        try:
            project = stage / "project"
            PortableProject._build(workspace, dependencies, project)
            if os.path.lexists(destination):
                raise FileExistsError(destination)
            os.rename(project, destination)
        finally:
            shutil.rmtree(stage)
        return destination

    @staticmethod
    def create_archive(workspace, dependencies, destination, *, overwrite=False):
        destination = Path(destination).absolute()
        dependencies = tuple(dependencies)
        PortableProject._check_destination(workspace, dependencies, destination)
        if os.path.lexists(destination) and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".manimble-package-", dir=destination.parent))
        try:
            project = stage / workspace.project_name
            PortableProject._build(workspace, dependencies, project)
            partial = stage / "archive.tmp"
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for file in sorted(project.rglob("*")):
                    if file.is_file():
                        archive.write(file, file.relative_to(stage).as_posix())
            if os.path.lexists(destination) and not overwrite:
                raise FileExistsError(destination)
            os.replace(partial, destination)
        finally:
            shutil.rmtree(stage)
        return destination


def new_deck_files(title, theme=None):
    common = '''"""Shared base class for the deck."""
from pathlib import Path
from manim import *
from manim_slides import Slide

ASSETS = Path(__file__).resolve().parent.parent / "assets"

class Deck(Slide):
    skip_reversing = True
'''
    metadata = {}
    if theme:
        # The engine parser reads constants via AST; theme source is never executed here.
        from manimble.theme import load
        metadata = load(theme) if isinstance(theme, str) else dict(theme)
        constants = "\n".join(f"{key} = {json.dumps(metadata[key], ensure_ascii=False)}" for key in
                              ("THEME", "BACKGROUND", "FONT", "TEXT_COLOR", "MUTED_COLOR", "ACCENT") if key in metadata)
        common = constants + "\n\n" + common
    title_style = ", font=FONT, color=TEXT_COLOR" if metadata else ""
    subtitle_style = ", font=FONT, color=MUTED_COLOR" if metadata else ", color=GREY_B"
    slides = f'''"""Slides of the deck."""
from common import *

class Title(Deck):
    def construct(self):
        title = Text({json.dumps(title, ensure_ascii=False)}, font_size=64{title_style}).move_to((0, 0.7, 0))  # manimble
        self.add(title)  # manimble
        subtitle = Text("Subtitle", font_size=32{subtitle_style}).move_to((0, -0.8, 0))  # manimble
        self.add(subtitle)  # manimble
'''
    return {"common.py": common, "slides.py": slides, "order.txt": "Title\n",
            "manim.cfg": f"[CLI]\nbackground_color = {metadata.get('BACKGROUND', '#000000')}\npixel_width = 1280\npixel_height = 720\nframe_rate = 30\n"}


def create_new_deck(destination, title=None, theme=None):
    destination = Path(destination).absolute()
    deck = destination / "deck"
    if os.path.lexists(deck):
        raise FileExistsError(f"A deck already exists at {deck}")
    files = new_deck_files(title or destination.name, theme)
    made_project = not destination.exists()
    made_assets = not (destination / "assets").exists()
    deck.mkdir(parents=True)
    try:
        (destination / "assets").mkdir(exist_ok=True)
        for name, text in files.items():
            (deck / name).write_text(text, encoding="utf-8")
    except BaseException:
        shutil.rmtree(deck)
        if made_assets:
            (destination / "assets").rmdir()
        if made_project:
            destination.rmdir()
        raise
    return deck


class RecentDecks:
    key = "recentDecks"
    limit = 10

    @staticmethod
    def _settings(settings=None):
        if settings is None:
            from .prefs import settings as current_settings
            return current_settings()
        return settings

    @classmethod
    def paths(cls, settings=None):
        settings = cls._settings(settings)
        value = settings.value(cls.key)
        if value is None:
            last = settings.value("lastDeck")
            return [str(last)] if last else []
        if isinstance(value, list):
            return value
        try:
            value = json.loads(str(value))
            return value if isinstance(value, list) else []
        except ValueError:
            return []

    @classmethod
    def note(cls, deck, settings=None):
        settings = cls._settings(settings)
        deck = str(Path(deck).resolve())
        settings.setValue(cls.key, json.dumps(([deck] + [p for p in cls.paths(settings) if p != deck])[:cls.limit]))
        settings.setValue("lastDeck", deck)

    @classmethod
    def forget(cls, deck, settings=None):
        settings = cls._settings(settings)
        settings.setValue(cls.key, json.dumps([p for p in cls.paths(settings) if p != str(deck)]))

    @classmethod
    def clear(cls, settings=None):
        cls._settings(settings).setValue(cls.key, "[]")


def selected_movie_scenes(scenes, text):
    picked, unknown = set(), []
    for token in filter(None, re.split(r"[,;\s]+", text.strip())):
        ends = re.fullmatch(r"(\d+)-(\d+)", token)
        if ends:
            a, b = map(int, ends.groups())
            low, high = max(1, min(a, b)), min(len(scenes), max(a, b))
            if low > high:
                unknown.append(token)
            else:
                picked.update(range(low - 1, high))
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= len(scenes):
                picked.add(n - 1)
            else:
                unknown.append(token)
        else:
            index = next((i for i, scene in enumerate(scenes) if scene["name"].lower() == token.lower()), None)
            if index is None:
                unknown.append(token)
            else:
                picked.add(index)
    if unknown:
        raise ValueError("No such slides: " + ", ".join(unknown) + f". Use navigator numbers (1–{len(scenes)}), names or ranges.")
    names = [scenes[i]["name"] for i in sorted(picked) if not scenes[i].get("hidden")]
    if not names:
        raise ValueError("Only hidden slides were chosen." if picked else "Type which slides to export, for example 1-3, 7 or Summary.")
    return names
