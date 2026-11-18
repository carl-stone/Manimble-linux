"""BioCartoon catalog, shared offline library, and editor insertion.

CLI and filesystem/HTTP methods ending in ``_sync`` run only in workers. Public
CLI methods return futures, consumed by the Qt task continuations.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field, fields, asdict, replace
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image, PngImagePlugin
from PySide6.QtCore import Qt, QMimeData, QPointF, QTimer, QSize
from PySide6.QtGui import QDrag, QPixmap, QIcon, QImage
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QLabel, QComboBox, QCheckBox, QListWidget, QListWidgetItem,
    QAbstractItemView, QDialog, QDialogButtonBox, QProgressDialog,
)

from .models import frame_size, unit_px
from .tasks import spawn, when_done
from .workers import run_blocking

DRAG_TYPE = "com.manimble.biocartoon-icon"
HTTP_HEADERS = {"User-Agent": "Manimble/BioCartoon"}


def _xdg(name, default):
    p = Path(os.environ.get(name, ""))
    return p if p.is_absolute() else Path.home() / default


def biocartoon_data():
    return _xdg("XDG_DATA_HOME", ".local/share") / "biocartoon"


def editor_settings():
    candidates = [
        _xdg("XDG_CONFIG_HOME", ".config") / "com.biocartoon.app/app.ron",
        _xdg("XDG_DATA_HOME", ".local/share") / "com.biocartoon.app/app.ron",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(errors="replace")
    return ""


def _setting(text, name):
    # The editor stores JSON inside a RON string, hence possibly escaped quotes.
    match = re.search(r'"' + re.escape(name) + r'\\?"\s*:\s*\\?"([^"\\]+)', text)
    return match.group(1) if match else None


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _json_http(url, payload=None):
    req = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                  headers=HTTP_HEADERS | ({"Content-Type": "application/json"} if payload is not None else {}))
    try:
        with urlopen(req, timeout=20) as reply:
            return json.load(reply)
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            try:
                answer = json.loads(exc.read())
            except ValueError:
                raise exc
            if isinstance(answer, dict) and isinstance(answer.get("error"), str):
                return answer
        raise


def _usage(file=None):
    p = Path(file) if file else biocartoon_data() / "usage.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=5)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("CREATE TABLE IF NOT EXISTS stars (id TEXT PRIMARY KEY, n INTEGER NOT NULL, mine INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY)")
    return db


@dataclass
class BCTaxon:
    category: str = ""
    subcategory: str = ""
    slug: str | None = None


@dataclass
class BCIcon:
    id: str
    title: str
    source: str
    source_name: str | None = None
    credit: str | None = None
    license: str | None = None
    category: str | None = None
    author: str | None = None
    url: str | None = None
    png_local: str | None = None
    png_key: str | None = None
    thumb_url: str | None = None
    width: float | None = None
    height: float | None = None
    svg_key: str | None = None
    variant: str | None = None
    taxa: list[BCTaxon] = field(default_factory=list)
    stars: int = 0
    starred: bool = False
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, value):
        names = {f.name for f in fields(cls)} - {"extra", "taxa"}
        obj = cls(**{k: v for k, v in value.items() if k in names})
        obj.taxa = [BCTaxon(**{k: v for k, v in t.items() if k in {"category", "subcategory", "slug"}})
                    for t in value.get("taxa") or []]
        obj.tags = value.get("tags") or []
        obj.extra = {k: v for k, v in value.items() if k not in names | {"taxa"}}
        return obj

    def to_dict(self):
        out = asdict(self)
        out.update(out.pop("extra"))
        return out

    @property
    def database(self):
        return "biocartoon" if self.source == "user" and self.author == "BioCartoon" else self.source

    @property
    def own(self):
        return self.source == "user" and bool(self.png_local)

    @property
    def categories(self):
        return [c.strip() for c in (self.category or "").split(", ") if c.strip()]

    @property
    def info(self):
        lines = [self.title]
        if self.variant:
            lines.append("Variant: " + self.variant)
        if self.taxa:
            lines.append("Category: " + ", ".join(t.category + (" › " + t.subcategory if t.subcategory else "") for t in self.taxa))
        if self.category:
            lines.append("Database category: " + self.category)
        details = [self.source_name or self.database, self.license or "", "SVG" if self.svg_key else "PNG"]
        if self.width and self.height:
            details.append(f"{int(self.width)} × {int(self.height)} px")
        lines += [" · ".join(x for x in details if x), self.credit or ""]
        return "\n".join(lines)


@dataclass
class BCCategory:
    slug: str
    name: str
    icons: int = 0


@dataclass
class BCSource:
    code: str
    name: str
    source: str
    icons: int = 0


@dataclass
class BCFilter:
    sources: set[str] = field(default_factory=set)
    authors: str = ""
    categories: set[str] = field(default_factory=set)
    subcategories: set[str] = field(default_factory=set)
    db_cats: set[str] = field(default_factory=set)
    licenses: set[str] = field(default_factory=set)
    file_types: set[str] = field(default_factory=set)
    tags: str = ""
    favorites: bool = False
    downloads: bool = False
    library = "library"

    @property
    def author_parts(self):
        return [x.strip().casefold() for x in self.authors.split(",") if x.strip()]

    @property
    def tag_list(self):
        return [x.strip() for x in self.tags.split(",") if x.strip()]

    @property
    def is_empty(self):
        return not any((self.sources, self.author_parts, self.categories, self.subcategories,
                        self.db_cats, self.licenses, self.file_types, self.tag_list, self.favorites, self.downloads))

    @staticmethod
    def key(database, category):
        return database + "\t" + category

    @staticmethod
    def split(key):
        a, _, b = key.partition("\t")
        return a, b

    def passes(self, icon, skip=None):
        taxa = {t.slug for t in icon.taxa if t.slug}
        taxa |= {t.split(".")[0] for t in taxa}
        return (
            (skip == "sources" or not self.sources or self.library in self.sources or icon.database in self.sources)
            and (not self.author_parts or any(p in (icon.author or "").casefold() for p in self.author_parts))
            and (skip == "taxa" or not self.categories | self.subcategories or bool(taxa & (self.categories | self.subcategories)))
            and (skip == "db_cats" or not self.db_cats or any(d == icon.database and c.casefold() in {x.casefold() for x in icon.categories}
                                                                       for d, c in map(self.split, self.db_cats)))
            and (skip == "licenses" or not self.licenses or (icon.license or "").casefold() in {l.casefold() for l in self.licenses})
            and (skip == "file_types" or not self.file_types or ("svg" if icon.svg_key else "png") in self.file_types)
            and (not self.tag_list or bool({t.casefold() for t in self.tag_list} & {t.casefold() for t in icon.tags}))
        )


@dataclass
class BCFacets:
    sources: list[dict] = field(default_factory=list)
    licenses: list[dict] = field(default_factory=list)
    categories: list[dict] = field(default_factory=list)
    source_categories: list[dict] = field(default_factory=list)
    file_types: list[dict] = field(default_factory=list)

    def source(self, key):
        return next((r["icons"] for r in self.sources if r.get("source") == key), 0)

    def taxon(self, slug):
        return next((r["icons"] for r in self.categories if r.get("slug") == slug), 0)

    def add(self, icons, under):
        def tally(rows, keys):
            for key in keys:
                row = next((r for r in rows if all(r.get(k) == v for k, v in key)), None)
                if row is None:
                    rows.append(dict(key, icons=1))
                else:
                    row["icons"] += 1
        for icon in icons:
            if under.passes(icon, "licenses") and icon.license:
                tally(self.licenses, {(('license', icon.license),)})
            if under.passes(icon, "taxa"):
                slugs = {t.slug for t in icon.taxa if t.slug}
                slugs |= {s.split(".")[0] for s in slugs}
                tally(self.categories, {(('slug', s),) for s in slugs})
            if under.passes(icon, "db_cats"):
                tally(self.source_categories, {(('source', icon.database), ('category', c)) for c in icon.categories})
            if under.passes(icon, "file_types"):
                tally(self.file_types, {(('file_type', "SVG" if icon.svg_key else "PNG"),)})


class BioCartoonCLI:
    minimum_version = "0.4.3"

    def __init__(self, runtime=None, executable=None, env=None):
        self.runtime = runtime
        self.env = dict(env if env is not None else runtime.child_env() if runtime else os.environ)
        self.exe = str(executable) if executable else None

    def find(self):
        explicit = self.env.get("MANIMBLE_BIOCARTOON")
        if explicit:
            p = Path(explicit).expanduser().absolute()
            if not p.is_file() or not os.access(p, os.X_OK):
                raise RuntimeError(f"MANIMBLE_BIOCARTOON is not executable: {p}")
            return str(p)
        candidates = []
        if self.runtime:
            candidates += [Path(self.runtime.python).parent / "biocartoon", self.runtime.paths.private_python.parent / "biocartoon"]
        path_hit = shutil.which("biocartoon", path=self.env.get("PATH"))
        if path_hit:
            candidates.append(Path(path_hit))
        return next((str(p.absolute()) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)

    @classmethod
    def supported(cls, version):
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
        return bool(match and tuple(map(int, match.groups())) >= (0, 4, 3))

    def run_sync(self, args, timeout=120):
        exe = self.exe or self.find()
        if not exe:
            raise RuntimeError('BioCartoon is missing. Run the Manimble installer, or install "biocartoon>=0.4.3" into the selected Python environment.')
        result = subprocess.run([exe, *map(str, args)], env=self.env, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or f"biocartoon exited {result.returncode}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def usable_sync(self):
        self.exe = self.exe or self.find()
        v = self.run_sync(["--version", "--json"])
        if not self.supported(v["version"]):
            raise RuntimeError(f'biocartoon {v["version"]} is too old; install biocartoon>=0.4.3 in Manimble’s Python.')
        return v

    def usable(self):
        return run_blocking(self.usable_sync)

    def version(self):
        return run_blocking(self.run_sync, ["--version", "--json"])

    def catalog_path_sync(self):
        return (self.run_sync(["--version", "--json"]).get("catalog") or {}).get("path")

    def catalog_path(self):
        return run_blocking(self.catalog_path_sync)

    @staticmethod
    def filter_args(f):
        out = []
        for values, flag in [(f.sources - {f.library}, "--source"), (f.author_parts, "--author"),
                             (f.categories, "--category"), (f.subcategories, "--subcategory"),
                             (f.licenses, "--license"), (f.file_types, "--file-type")]:
            for value in sorted(values):
                out += [flag, value]
        for key in sorted(f.db_cats):
            d, c = f.split(key)
            out += ["--source-category", f"{d}:{c}"]
        if f.tag_list:
            out += ["--tags", ",".join(f.tag_list)]
        if f.favorites:
            out.append("--favorites")
        if f.downloads:
            out.append("--downloads")
        return out

    def search_page_sync(self, query, filter, sort, offset, limit):
        args = ["search"] + ([query] if query else [])
        args += ["--icons", "--no-sql", "--json", "--limit", str(limit), "--offset", str(offset), "--sort", sort]
        return [BCIcon.from_dict(i) for i in (self.run_sync(args + self.filter_args(filter), 300).get("icons") or [])]

    def own_icons_sync(self, query, sort="relevance", mine=None):
        f = replace(mine or BCFilter(), sources={"user", "biocartoon"})
        return [i for i in self.search_page_sync(query, f, sort, 0, 5000) if i.own]

    def search_sync(self, query="", filter=None, sort="relevance", offset=0, limit=60):
        f = filter or BCFilter()
        if f.library not in f.sources:
            return self.search_page_sync(query, f, sort, offset, limit)
        dbs = replace(f, sources=f.sources - {f.library})
        extra = [i for i in self.own_icons_sync(query, sort, f) if f.passes(i) and i.database not in dbs.sources]
        mine = extra[offset:offset + limit]
        if not dbs.sources or len(mine) == limit:
            return mine
        return mine + self.search_page_sync(query, dbs, sort, max(0, offset - len(extra)), limit - len(mine))

    def search(self, query="", filter=None, sort="relevance", offset=0, limit=60):
        return run_blocking(self.search_sync, query, filter, sort, offset, limit)

    def facets_sync(self, query="", filter=None):
        f = filter or BCFilter()
        dbs = replace(f, sources=f.sources - {f.library})
        args = ["search"] + ([query] if query else []) + ["--facets", "--icons", "--no-sql", "--json"]
        fc = BCFacets(**self.run_sync(args + self.filter_args(dbs)))
        own = self.own_icons_sync(query, mine=f)
        if f.library in f.sources:
            if not dbs.sources:
                fc.licenses, fc.categories, fc.source_categories, fc.file_types = [], [], [], []
            fc.add([i for i in own if i.database not in dbs.sources], f)
        fc.sources.append({"source": f.library, "icons": sum(f.passes(i, "sources") for i in own)})
        return fc

    def facets(self, query="", filter=None):
        return run_blocking(self.facets_sync, query, filter)

    def sources(self):
        return run_blocking(lambda: [BCSource(**{k: v for k, v in r.items() if k in {f.name for f in fields(BCSource)}})
                                     for r in self.run_sync(["--sources", "--json"])])

    def categories(self):
        return run_blocking(lambda: [BCCategory(**{k: v for k, v in r.items() if k in {"slug", "name", "icons"}})
                                     for r in self.run_sync(["--categories", "--json"])])

    def source_categories(self):
        return run_blocking(self.run_sync, ["--source-categories", "--json"])

    def sql_sync(self, statement, max_rows=4000):
        return self.run_sync(["search", statement, "--sql", "--json", "--max-rows", str(max_rows)])["rows"]

    def sql(self, statement, max_rows=4000):
        return run_blocking(self.sql_sync, statement, max_rows)

    def sync(self):
        return run_blocking(self.run_sync, ["sync", "--json"], 600)

    def fetch_sync(self, id, to, max_side=1600, svg=False):
        args = ["fetch", id, "--out", str(to), "--json"]
        args += ["--svg"] if svg else ["--max-side", str(max_side)]
        self.run_sync(args)
        return Path(to)

    def fetch(self, id, to, max_side=1600):
        return run_blocking(self.fetch_sync, id, to, max_side)

    def fetch_svg(self, id, to):
        return run_blocking(self.fetch_sync, id, to, 1600, True)

    @staticmethod
    def trim_transparent_margins(url):
        with Image.open(url) as src:
            image = src.convert("RGBA")
        w, h = image.size
        box = image.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
        if box:
            x0, y0, x1, y1 = box
            box = max(0, x0 - 2), max(0, y0 - 2), min(w, x1 + 2), min(h, y1 + 2)
            if (box[2] - box[0]) * (box[3] - box[1]) < .95 * w * h:
                image = image.crop(box)
                out = BytesIO()
                image.save(out, "PNG")
                atomic_bytes(url, out.getvalue())
        return image.size

    @staticmethod
    def tag(url, icon):
        with Image.open(url) as src:
            image = src.copy()
            info = PngImagePlugin.PngInfo()
            for k, v in src.info.items():
                if isinstance(v, str):
                    info.add_itxt(k, v)
        for k, v in {"Title": icon.title, "Comment": "biocartoon:" + json.dumps(icon.to_dict(), ensure_ascii=False),
                     "Description": icon.credit, "Author": icon.author, "Copyright": icon.license}.items():
            if v:
                info.add_itxt(k, v)
        out = BytesIO()
        image.save(out, "PNG", pnginfo=info)
        atomic_bytes(url, out.getvalue())

    @staticmethod
    def tag_svg(url, icon):
        encoded = base64.b64encode(json.dumps(icon.to_dict(), ensure_ascii=False).encode()).decode()
        atomic_bytes(url, Path(url).read_bytes() + f"\n<!-- biocartoon64:{encoded} -->\n".encode())

    @staticmethod
    def origin(path):
        try:
            if str(path).lower().endswith(".svg"):
                match = re.findall(r"<!-- biocartoon64:([A-Za-z0-9+/=]+) -->", Path(path).read_text())
                return BCIcon.from_dict(json.loads(base64.b64decode(match[-1]))) if match else None
            with Image.open(path) as image:
                for key in ("Comment", "comment", "UserComment"):
                    value = image.info.get(key, "")
                    if isinstance(value, str) and value.startswith("biocartoon:"):
                        return BCIcon.from_dict(json.loads(value[11:]))
        except (OSError, ValueError, KeyError):
            pass
        return None


class BCDownloads:
    @staticmethod
    def record(ids, usage_file=None):
        with _usage(usage_file) as db:
            db.executemany("INSERT OR IGNORE INTO downloads(id) VALUES (?)", ((i,) for i in ids if i))

    @classmethod
    def add(cls, icon):
        return run_blocking(cls.record, [icon.id])

    @staticmethod
    def import_editor():
        match = re.search(r'downloads\\?"\s*:\s*(\[[^]]*\])', editor_settings())
        if match:
            BCDownloads.record(json.loads(match.group(1).replace('\\"', '"')))

    @staticmethod
    def ids(usage_file=None):
        with _usage(usage_file) as db:
            return {r[0] for r in db.execute("SELECT id FROM downloads")}


class BCStars:
    def __init__(self, settings=None, usage_file=None, server=None):
        self.settings = settings
        self.usage_file = usage_file
        self.server = (server or os.environ.get("BIOCARTOON_SUBMIT_URL") or "https://api.biocartoon.com").rstrip("/ ")
        self.counts, self.mine = {}, set()
        self.on_problem = None
        self.on_change = None
        self.lock = threading.RLock()
        self.last_sync = 0
        self.client = str(settings.value("biocartoonClientId", "")) if settings else ""
        if not re.fullmatch("[0-9a-f]{32}", self.client):
            self.client = secrets.token_hex(16)
            if settings:
                settings.setValue("biocartoonClientId", self.client)

    def count(self, icon):
        return self.counts.get(icon.id, icon.stars or 0)

    def is_starred(self, icon):
        return icon.id in self.mine if icon.id in self.counts else bool(icon.starred)

    @staticmethod
    def format(n):
        n = max(0, n)
        for suffix, size in [("B", 10**9), ("M", 10**6), ("k", 1000)]:
            if n >= size:
                whole, tenth = n // size, n % size // (size // 10)
                return f"{whole}.{tenth}{suffix}" if whole < 10 and tenth else f"{whole}{suffix}"
        return str(n)

    def _db(self):
        db = _usage(self.usage_file)
        db.execute("CREATE TABLE IF NOT EXISTS manimble_pending_stars(id TEXT PRIMARY KEY, on_value INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS manimble_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        return db

    def load_sync(self):
        with self.lock, self._db() as db:
            shared = _setting(editor_settings(), "client_id")
            saved = db.execute("SELECT value FROM manimble_meta WHERE key='client_id'").fetchone()
            self.client = shared if shared and re.fullmatch("[0-9a-f]{32}", shared) else saved[0] if saved else self.client
            db.execute("INSERT OR REPLACE INTO manimble_meta VALUES ('client_id', ?)", (self.client,))
            self.counts = {i: n for i, n, m in db.execute("SELECT id,n,mine FROM stars")}
            self.mine = {i for i, n, m in db.execute("SELECT id,n,mine FROM stars") if m}
        return self.counts.copy(), self.mine.copy()

    def _send_pending(self):
        with self._db() as db:
            pending = list(db.execute("SELECT id,on_value FROM manimble_pending_stars"))
        for id, on in pending:
            answer = _json_http(self.server + "/star", {"id": id, "client": self.client, "on": bool(on)})
            if "error" in answer:
                if str(answer["error"]).startswith("Limit reached"):
                    raise RuntimeError("Your star will be counted later. " + answer["error"])
                with self._db() as db:
                    db.execute("DELETE FROM manimble_pending_stars WHERE id=? AND on_value=?", (id, on))
                continue
            if not isinstance(answer.get("stars"), int):
                raise RuntimeError("Unexpected BioCartoon star response")
            with self._db() as db:
                db.execute("DELETE FROM manimble_pending_stars WHERE id=? AND on_value=?", (id, on))
                self.counts[id] = answer["stars"]
                if answer.get("starred", bool(on)):
                    self.mine.add(id)
                else:
                    self.mine.discard(id)
                db.execute("INSERT OR REPLACE INTO stars VALUES(?,?,?)", (id, self.counts[id], int(id in self.mine)))

    def toggle_sync(self, icon):
        with self.lock:
            on = not self.is_starred(icon)
            self.counts[icon.id] = max(0, self.count(icon) + (1 if on else -1))
            if on:
                self.mine.add(icon.id)
            else:
                self.mine.discard(icon.id)
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO stars VALUES(?,?,?)", (icon.id, self.counts[icon.id], int(on)))
                db.execute("INSERT OR REPLACE INTO manimble_pending_stars VALUES(?,?)", (icon.id, int(on)))
            self._send_pending()
        return self.counts.copy(), self.mine.copy()

    def sync_sync(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_sync < 300:
                return self.counts.copy(), self.mine.copy()
            fresh = {k: int(v) for k, v in _json_http(self.server + "/stars/all").items()}
            mine_now = set()
            ids = sorted(fresh)
            for start in range(0, len(ids), 90):
                answer = _json_http(self.server + "/stars?" + urlencode({"ids": ",".join(ids[start:start + 90]), "client": self.client}))
                mine_now.update(answer.get("mine", []))
            with self._db() as db:
                # Own, locally starred uploads remain even when the server does not know them.
                for id, n, m in db.execute("SELECT id,n,mine FROM stars WHERE mine=1"):
                    if id not in fresh:
                        fresh[id] = n
                        mine_now.add(id)
                pending = dict(db.execute("SELECT id,on_value FROM manimble_pending_stars"))
                for id, on in pending.items():
                    fresh[id] = max(0, fresh.get(id, 0) + (0 if (id in mine_now) == bool(on) else 1 if on else -1))
                    if on:
                        mine_now.add(id)
                    else:
                        mine_now.discard(id)
                db.execute("UPDATE stars SET n=0 WHERE mine=0")
                db.executemany("INSERT OR REPLACE INTO stars VALUES(?,?,?)", ((i, n, int(i in mine_now)) for i, n in fresh.items()))
            self.counts, self.mine = fresh, mine_now
            self.last_sync = time.monotonic()
            self._send_pending()
        return self.counts.copy(), self.mine.copy()

    def _notify(self, future):
        def complete(f):
            try:
                f.result()
            except Exception as exc:
                if self.on_problem:
                    self.on_problem(str(exc))
            if self.on_change:
                self.on_change()
        when_done(future, complete)
        return future

    def toggle(self, icon):
        return self._notify(run_blocking(self.toggle_sync, icon))

    def sync(self, force=False):
        return self._notify(run_blocking(self.sync_sync, force))


class BCOffline:
    def __init__(self, folder=None, asset_base=None):
        self.folder = Path(folder) if folder else None
        self.asset_base = (asset_base or os.environ.get("BIOCARTOON_ASSET_URL") or "https://assets.biocartoon.com").rstrip("/")
        self.cancelled = threading.Event()

    def locate(self):
        if self.folder is None:
            saved = _setting(editor_settings(), "offline_dir")
            self.folder = Path(saved) if saved and Path(saved).is_absolute() else biocartoon_data() / "offline"
        return self.folder

    @property
    def assets(self):
        return self.locate() / "assets"

    def file(self, key):
        if isinstance(key, BCIcon):
            key = key.png_key or (key.url[len(self.asset_base) + 1:] if key.url and key.url.startswith(self.asset_base + "/") else "")
        if not key or "\\" in key or "\x00" in key or key.startswith("/"):
            return None
        parts = key.split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        target = self.assets / PurePosixPath(key)
        if not target.resolve().is_relative_to(self.assets.resolve()):
            return None
        return target

    def saved(self, icon):
        p = self.file(icon)
        return p if p and p.is_file() else None

    def fetch(self, key, dest=None):
        dest = Path(dest) if dest else self.file(key)
        if dest is None:
            raise ValueError(f"Unsafe offline asset key: {key}")
        if self.cancelled.is_set():
            raise CancelledError()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + dest.name, suffix=".part", dir=dest.parent)
        try:
            req = Request(self.asset_base + "/" + quote(key, safe="/"), headers=HTTP_HEADERS)
            with os.fdopen(fd, "wb") as out, urlopen(req, timeout=60) as reply:
                while True:
                    if self.cancelled.is_set():
                        raise CancelledError()
                    chunk = reply.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
            with Image.open(tmp) as image:
                if image.format != "PNG":
                    raise ValueError(f"{key} is not a PNG")
                image.verify()
            if self.cancelled.is_set():
                raise CancelledError()
            os.replace(tmp, dest)
            return dest
        finally:
            Path(tmp).unlink(missing_ok=True)

    def save(self, icon):
        if self.saved(icon):
            return self.saved(icon)
        p = self.file(icon)
        if p is None:
            raise ValueError(f"{icon.title} has no safe PNG key")
        key = p.relative_to(self.assets).as_posix()
        return self.fetch(key, p)

    def copy(self, icon, to, max_side=1600):
        p = Path(icon.png_local) if icon.png_local else self.saved(icon)
        if not p:
            return False
        with Image.open(p) as src:
            image = src.convert("RGBA")
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.save(out, "PNG")
            atomic_bytes(to, out.getvalue())
        return True

    def missing_sync(self, cli):
        rows = cli.sql_sync("SELECT png_key,coalesce(png_bytes,0) FROM assets WHERE png_key IS NOT NULL AND png_key != '' ORDER BY source,png_key", 1_000_000)
        missing = [(k, n) for k, n in rows if self.file(k) and not self.file(k).is_file()]
        return {"keys": list(dict.fromkeys(k for k, n in missing)), "bytes": sum(n for k, n in missing), "total": len(rows)}

    def record_downloaded_sync(self, cli):
        rows = cli.sql_sync("SELECT id,png_key FROM assets WHERE png_key IS NOT NULL", 1_000_000)
        BCDownloads.record([id for id, key in rows if self.file(key) and self.file(key).is_file()])

    def download(self, keys, progress=None):
        keys = iter(dict.fromkeys(keys))
        got = failed = 0

        def one(key):
            if self.file(key) is None:
                raise ValueError(f"Unsafe offline asset key: {key}")
            if self.file(key).is_file():
                return True
            for attempt in range(3):
                if self.cancelled.is_set():
                    raise CancelledError()
                try:
                    self.fetch(key)
                    return True
                except CancelledError:
                    raise
                except Exception:
                    if attempt == 2:
                        raise
                    if self.cancelled.wait(.5 * 2**attempt):
                        raise CancelledError()

        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="biocartoon-offline") as pool:
            pending = set()
            def add():
                if not self.cancelled.is_set():
                    key = next(keys, None)
                    if key is not None:
                        pending.add(pool.submit(one, key))
            for _ in range(8):
                add()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    try:
                        f.result()
                        got += 1
                    except CancelledError:
                        continue
                    except Exception:
                        failed += 1
                    if progress:
                        progress(got, failed)
                    add()
        return {"got": got, "failed": failed, "cancelled": self.cancelled.is_set()}

    def cancel(self):
        self.cancelled.set()


class BCThumbs:
    def __init__(self, offline=None, max_images=160):
        self.offline = offline or BCOffline()
        self.cache, self.waiting = OrderedDict(), {}
        self.max_images = max_images

    def _read(self, icon):
        local = Path(icon.png_local) if icon.png_local else self.offline.saved(icon)
        if not local and icon.url and icon.url.startswith("/"):
            local = Path(icon.url)
        data = local.read_bytes() if local else None
        if data is None:
            last_error = None
            for url in dict.fromkeys(u for u in (icon.thumb_url, icon.url) if u and u.startswith(("https://", "http://"))):
                try:
                    with urlopen(Request(url, headers=HTTP_HEADERS), timeout=30) as reply:
                        data = reply.read()
                    break
                except OSError as exc:
                    last_error = exc
            if data is None:
                raise last_error or ValueError("This icon has no preview")
        with Image.open(BytesIO(data)) as src:
            image = src.convert("RGBA")
            image.thumbnail((240, 240), Image.Resampling.LANCZOS)
            out = BytesIO()
            image.save(out, "PNG")
            return out.getvalue()

    def image(self, icon, done):
        if icon.id in self.cache:
            self.cache.move_to_end(icon.id)
            done(self.cache[icon.id], None)
            return
        if icon.id in self.waiting:
            self.waiting[icon.id].append(done)
            return
        self.waiting[icon.id] = [done]
        def completed(f):
            image, error = None, None
            try:
                image = QImage.fromData(f.result())
                self.cache[icon.id] = image
                while len(self.cache) > self.max_images:
                    self.cache.popitem(last=False)
            except Exception as exc:
                error = str(exc)
            for cb in self.waiting.pop(icon.id, []):
                cb(image, error)
        when_done(run_blocking(self._read, icon), completed)


class BCMultiPopup(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.picked = set()
        self.names = {}
        self.on_change = None
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.activated.connect(self.toggled)

    def set_choices(self, choices):
        self.blockSignals(True)
        self.clear()
        self.addItem("Any", None)
        self.names = {}
        for key, name, count in choices:
            self.names[key] = name
            self.addItem(name + (f" ({count:,})" if count is not None else ""), key)
        self.picked &= self.names.keys()
        self.refresh_title()
        self.blockSignals(False)

    def pick(self, keys):
        self.picked = set(keys) & self.names.keys()
        self.refresh_title()

    def toggled(self, index):
        key = self.itemData(index)
        if key is None:
            self.picked.clear()
        elif key in self.picked:
            self.picked.remove(key)
        else:
            self.picked.add(key)
        self.refresh_title()
        if self.on_change:
            self.on_change()

    def refresh_title(self):
        for i in range(1, self.count()):
            self.setItemData(i, Qt.CheckState.Checked if self.itemData(i) in self.picked else Qt.CheckState.Unchecked,
                             Qt.ItemDataRole.CheckStateRole)
        self.setCurrentIndex(0)
        self.lineEdit().setText(", ".join(self.names[k] for k in self.names if k in self.picked) or "Any")


class BCInfoTip(QDialog):
    def __init__(self, icon, image=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(icon.title)
        layout = QVBoxLayout(self)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if image is not None:
            preview.setPixmap(QPixmap.fromImage(image).scaled(400, 400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(preview)
        label = QLabel(icon.info)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        copy = QPushButton("Copy Credit")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(icon.credit or icon.title))
        layout.addWidget(copy)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)


class BCTile(QListWidget):
    """Icon grid and drag source; the payload is exactly Swift's BCIcon JSON."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(QSize(100, 100))
        self.setGridSize(QSize(128, 155))
        self.setWordWrap(True)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        icon = item.data(Qt.ItemDataRole.UserRole)
        mime = QMimeData()
        mime.setData(DRAG_TYPE, json.dumps(icon.to_dict(), ensure_ascii=False).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(item.icon().pixmap(96, 96))
        drag.exec(Qt.DropAction.CopyAction)


class BioCartoonPane(QWidget):
    drag_type = DRAG_TYPE

    def __init__(self, runtime=None, settings=None, parent=None):
        super().__init__(parent)
        self.view = self
        self.cli = BioCartoonCLI(runtime)
        self.exe = None
        self.on_insert = self.on_close = None
        self.filter = BCFilter()
        self.offline = BCOffline()
        self.thumbs = BCThumbs(self.offline)
        self.stars = BCStars(settings)
        self.stars.on_problem = self._problem
        self.stars.on_change = self._stars_changed
        self.all_results = []
        self.offset = self.generation = 0
        self.is_loading = False
        self.facets = None
        self.facets_settled = False
        self._active = False
        self.page_size = 60
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("BioCartoon Icons"))
        close = QPushButton("Close")
        close.clicked.connect(lambda: self.on_close() if self.on_close else self.hide())
        header.addWidget(close)
        layout.addLayout(header)
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search science icons…")
        layout.addWidget(self.search_field)
        self.typing = QTimer(self)
        self.typing.setSingleShot(True)
        self.typing.setInterval(300)
        self.typing.timeout.connect(self.reload)
        self.search_field.textChanged.connect(lambda: self.typing.start())
        self.search_field.returnPressed.connect(self.reload)
        form = QFormLayout()
        self.popups = {}
        for key, label in [("sources", "Database"), ("categories", "Category"), ("subcategories", "Subcategory"),
                           ("db_cats", "Database category"), ("licenses", "License"), ("file_types", "File type")]:
            popup = BCMultiPopup()
            popup.on_change = self._filters_changed
            self.popups[key] = popup
            form.addRow(label, popup)
        self.authors = QLineEdit()
        self.authors.setPlaceholderText("names, separated by commas")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("tags, separated by commas")
        for key, widget in [("Authors", self.authors), ("Tags", self.tags)]:
            form.addRow(key, widget)
            widget.editingFinished.connect(self._filters_changed)
        self.sort = QComboBox()
        for label, value in [("Popular", "popular"), ("Best match", "relevance"), ("A–Z", "az"), ("Z–A", "za")]:
            self.sort.addItem(label, value)
        self.sort.currentIndexChanged.connect(self.reload)
        form.addRow("Sort", self.sort)
        layout.addLayout(form)
        toggles = QHBoxLayout()
        self.favorites, self.downloads = QCheckBox("Favorites"), QCheckBox("Downloaded")
        for cb in (self.favorites, self.downloads):
            cb.toggled.connect(self._filters_changed)
            toggles.addWidget(cb)
        reset = QPushButton("Clear Filters")
        reset.clicked.connect(lambda: self.apply(BCFilter()))
        toggles.addWidget(reset)
        layout.addLayout(toggles)
        self.tiles = BCTile()
        self.tiles.itemClicked.connect(lambda item: self.on_insert(item.data(Qt.ItemDataRole.UserRole)) if self.on_insert else None)
        self.tiles.currentItemChanged.connect(lambda *_: self._selection_changed())
        layout.addWidget(self.tiles, 1)
        controls = QHBoxLayout()
        for text, fn in [("Preview / Credit", self.preview), ("☆ Star", self.toggle_star), ("Download", self.download_selected)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            controls.addWidget(b)
        layout.addLayout(controls)
        footer = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.previous.clicked.connect(lambda: self._page(-1))
        self.next.clicked.connect(lambda: self._page(1))
        footer.addWidget(self.previous)
        footer.addWidget(self.next)
        sync = QPushButton("Sync")
        sync.clicked.connect(lambda: spawn(self._sync()))
        footer.addWidget(sync)
        bulk = QPushButton("Offline…")
        bulk.clicked.connect(lambda: spawn(self._bulk()))
        footer.addWidget(bulk)
        layout.addLayout(footer)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

    @property
    def result_count(self):
        return len(self.all_results)

    @property
    def first_result(self):
        return next(iter(self.all_results), None)

    def _problem(self, text):
        self.status.setText(str(text))

    def activate(self, within=None):
        self.search_field.setFocus()
        if not self._active:
            self._active = True
            spawn(self._activate())

    def _activate(self):
        try:
            yield self.cli.usable()
            self.exe = self.cli.exe
            self.sources = yield self.cli.sources()
            self.categories = yield self.cli.categories()
            self.source_categories = yield self.cli.source_categories()
            yield run_blocking(BCDownloads.import_editor)
            yield run_blocking(self.stars.load_sync)
            self.reload()
            self.stars.sync()
        except Exception as exc:
            self._active = False
            self._problem(exc)

    def _sync(self):
        try:
            self.status.setText("Updating catalog…")
            yield self.cli.sync()
            self.stars.sync(True)
            self.reload()
        except Exception as exc:
            self._problem(exc)

    def type(self, text):
        self.search_field.setText(text)

    def apply(self, f):
        self.filter = f
        for key, popup in self.popups.items():
            popup.pick(getattr(f, key))
        self.authors.setText(f.authors)
        self.tags.setText(f.tags)
        for box, value in [(self.favorites, f.favorites), (self.downloads, f.downloads)]:
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        self.reload()

    def _filters_changed(self):
        self.filter = BCFilter(**{k: p.picked.copy() for k, p in self.popups.items()},
                               authors=self.authors.text(), tags=self.tags.text(),
                               favorites=self.favorites.isChecked(), downloads=self.downloads.isChecked())
        self.reload()

    def reload(self):
        self.offset = 0
        self._load()

    def _page(self, delta):
        self.offset = max(0, self.offset + delta * self.page_size)
        self._load()

    def _load(self):
        if not self.exe:
            return
        self.typing.stop()
        self.generation += 1
        spawn(self._load_task(self.generation, self.filter, self.search_field.text(), self.offset))

    def _load_task(self, generation, f, query, offset):
        self.is_loading = True
        self.facets_settled = False
        self.status.setText("Searching…")
        sort = self.sort.currentData()
        try:
            rows = yield self.cli.search(query, f, sort, offset, self.page_size)
            downloaded = yield run_blocking(BCDownloads.ids)
            if generation != self.generation:
                return
            self.all_results = rows
            self._downloaded = downloaded
            self.tiles.clear()
            for icon in rows:
                item = QListWidgetItem(self._tile_title(icon))
                item.setData(Qt.ItemDataRole.UserRole, icon)
                item.setToolTip(icon.info)
                self.tiles.addItem(item)
                def done(image, error, item=item, generation=generation):
                    if generation != self.generation:
                        return
                    if image is not None:
                        item.setIcon(QIcon(QPixmap.fromImage(image)))
                    elif error:
                        item.setToolTip(item.toolTip() + "\nPreview error: " + error)
                self.thumbs.image(icon, done)
            self.previous.setEnabled(offset > 0)
            self.next.setEnabled(len(rows) == self.page_size)
            self.status.setText(f"{offset + 1}–{offset + len(rows)}" if rows else "No icons found")
            fc = yield self.cli.facets(query, f)
            if generation != self.generation:
                return
            self.facets = fc
            self._choices(fc)
        except Exception as exc:
            if generation == self.generation:
                self._problem(exc)
        finally:
            if generation == self.generation:
                self.is_loading = False
                self.facets_settled = True

    def _choices(self, fc):
        names = {s.source: s.name for s in self.sources}
        names[BCFilter.library] = "Your library"
        counts = {r["source"]: r["icons"] for r in fc.sources}
        self.popups["sources"].set_choices([(k, n, counts.get(k, 0)) for k, n in names.items()])
        for key, is_sub in [("categories", False), ("subcategories", True)]:
            self.popups[key].set_choices([(c.slug, c.name, fc.taxon(c.slug)) for c in self.categories if ("." in c.slug) == is_sub])
        db_counts = {(r["source"], r["category"]): r["icons"] for r in fc.source_categories}
        self.popups["db_cats"].set_choices([(BCFilter.key(r["source"], r["category"]), names.get(r["source"], r["source"]) + ": " + r["category"],
                                          db_counts.get((r["source"], r["category"]), 0)) for r in self.source_categories])
        for key, rows, field_name in [("licenses", fc.licenses, "license"), ("file_types", fc.file_types, "file_type")]:
            self.popups[key].set_choices([(r[field_name].lower() if key == "file_types" else r[field_name], r[field_name], r["icons"]) for r in rows])
        for key, popup in self.popups.items():
            popup.pick(getattr(self.filter, key))

    def _tile_title(self, icon):
        star = "★" if self.stars.is_starred(icon) else "☆"
        return f"{icon.title}\n{star} {BCStars.format(self.stars.count(icon))}" + ("  ↓" if icon.id in getattr(self, "_downloaded", set()) else "")

    def _stars_changed(self):
        for n in range(self.tiles.count()):
            item = self.tiles.item(n)
            item.setText(self._tile_title(item.data(Qt.ItemDataRole.UserRole)))
        if self.filter.favorites:
            self.reload()

    def _selection_changed(self):
        icon = self._selected()
        if icon:
            self.status.setText(icon.credit or icon.title)

    def _selected(self):
        item = self.tiles.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def toggle_star(self):
        if icon := self._selected():
            self.stars.toggle(icon)

    def preview(self):
        if icon := self._selected():
            def done(image, error):
                dialog = BCInfoTip(icon, image, self)
                dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                dialog.show()
            self.thumbs.image(icon, done)

    def download_selected(self):
        if icon := self._selected():
            spawn(self._download_one(icon))

    def _download_one(self, icon):
        try:
            yield run_blocking(self.offline.save, icon)
            yield BCDownloads.add(icon)
            self._downloaded = getattr(self, "_downloaded", set()) | {icon.id}
            self._stars_changed()
            self.status.setText("Downloaded " + icon.title)
        except Exception as exc:
            self._problem(exc)

    def _bulk(self):
        try:
            batch = BCOffline()
            missing = yield run_blocking(batch.missing_sync, self.cli)
            from PySide6.QtWidgets import QMessageBox
            answer = QMessageBox.question(self, "Download catalog for offline use?",
                f'{len(missing["keys"]):,} PNGs, approximately {missing["bytes"] / 1e6:.1f} MB.\nFolder: {batch.locate()}\nUses eight download workers. Existing files are retained.',
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return
            progress = QProgressDialog("Downloading icons…", "Cancel", 0, len(missing["keys"]), self)
            progress.canceled.connect(batch.cancel)
            progress.show()
            from .tasks import post
            def update(got, failed):
                post(lambda: (progress.setValue(got + failed), progress.setLabelText(f"Downloaded {got}; failed {failed}")))
            result = yield run_blocking(batch.download, missing["keys"], update)
            yield run_blocking(batch.record_downloaded_sync, self.cli)
            self._downloaded = yield run_blocking(BCDownloads.ids)
            self._stars_changed()
            progress.close()
            self.status.setText(f'Downloaded {result["got"]}; failed {result["failed"]}' + ("; cancelled" if result["cancelled"] else ""))
        except Exception as exc:
            self._problem(exc)


class BioCartoonMixin:
    def show_bio_cartoon(self):
        pane = getattr(self, "biocartoon_pane", None)
        if pane and pane.isVisible():
            self.hide_bio_cartoon()
            return
        if pane is None:
            pane = BioCartoonPane(self.runtime, self.settings, self.window)
            pane.on_insert = lambda icon: self.insert_bio_cartoon(icon, None)
            pane.on_close = self.hide_bio_cartoon
            self.biocartoon_pane = pane
        self.canvas.on_icon_drop = self.biocartoon_dropped
        self.show_inspector_page(pane)
        pane.activate(self.window)

    def hide_bio_cartoon(self):
        self.show_inspector_page(None)

    def biocartoon_dropped(self, data, at):
        if not self.scene_name or self.player:
            return False
        try:
            icon = BCIcon.from_dict(json.loads(bytes(data)))
        except (ValueError, KeyError, TypeError):
            return False
        self.insert_bio_cartoon(icon, at)
        return True

    def insert_bio_cartoon(self, icon, at=None):
        if not self.work_deck or not self.scene_name or self.player:
            return
        # Capture now, before download; submit retains this target while navigation continues.
        context = self.edit_context
        deck = self.work_deck
        size = frame_size(self.scene)
        unit = unit_px(self.scene)
        point = at or QPointF(size[0] / 2, size[1] / 2)
        def work():
            try:
                cli = getattr(getattr(self, "biocartoon_pane", None), "cli", None) or BioCartoonCLI(self.runtime)
                yield cli.usable()
                png, svg = yield run_blocking(self._bio_paths, deck, icon)
                offline = BCOffline()
                copied = yield run_blocking(offline.copy, icon, png)
                if not copied:
                    yield cli.fetch(icon.id, png)
                yield run_blocking(BioCartoonCLI.tag, png, icon)
                dest = png
                dims = None
                if icon.svg_key:
                    try:
                        raw = svg.with_name("." + svg.name + ".raw")
                        try:
                            yield cli.fetch_svg(icon.id, raw)
                            reply = yield self.call("svg_icon", {"svg": str(raw), "png": str(png), "out": str(svg)})
                            if reply.get("ok") and reply.get("width", 0) > 0 and reply.get("height", 0) > 0:
                                yield run_blocking(BioCartoonCLI.tag_svg, svg, icon)
                                dims = reply["width"], reply["height"]
                                dest = svg
                            else:
                                self.toast("Keeping PNG: " + reply.get("why", "the SVG does not match its PNG"))
                        finally:
                            yield run_blocking(raw.unlink, missing_ok=True)
                    except Exception as exc:
                        self.toast(f"Keeping PNG: {exc}")
                if dims is None:
                    yield run_blocking(svg.unlink, missing_ok=True)
                    dims = yield run_blocking(BioCartoonCLI.trim_transparent_margins, png)
                    yield run_blocking(BioCartoonCLI.tag, png, icon)
                half = 1.5 * unit * min(1, dims[0] / max(dims[1], 1))
                if self.work_deck != deck:
                    raise RuntimeError("The deck changed while the icon was downloading; nothing was inserted")
                req = {"scene": context.scene, "stop": context.stop, "seen": context.seen,
                       "items": [{"kind": "image", "file": dest.name, "path": str(dest),
                                  "sx": point.x() - half, "sy": point.y(), "ex": point.x() + half, "ey": point.y()}]}
                reply = yield self.call("insert_many", req)
                if not reply.get("ok"):
                    raise RuntimeError(reply.get("error", "Could not insert icon"))
                yield BCDownloads.add(icon)
                yield from self.reload_state()
                if self.scene_name == context.scene:
                    yield from self.load_scene(context.scene, True)
                self.toast("Inserted " + icon.title)
            except Exception as exc:
                self.toast(str(exc), error=True)
        return self.submit(work)

    @staticmethod
    def _bio_paths(deck, icon):
        stem = "_".join(re.findall(r"[^\W_]+", icon.title.lower())[:6]) or "biocartoon"
        for n in range(100000):
            base = stem + (f"_{n}" if n else "")
            png, svg = Path(deck) / (base + ".png"), Path(deck) / (base + ".svg")
            if not png.exists() and not svg.exists():
                return png, svg
        raise RuntimeError("Could not allocate an icon filename")

    def show_bio_cartoon_credit(self, icon):
        dialog = BCInfoTip(icon, parent=self.window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def bio_cartoon_menu_items(self):
        obj = self.obj_info or {}
        path = obj.get("imagePath") or obj.get("image_path")
        if obj.get("kind") != "image" or not path:
            return []
        # Menu construction belongs to the registry; return descriptive commands.
        return [("Show Credit…", lambda: spawn(self._show_credit_or_library(path)))] + self.bio_cartoon_library_items(path)

    def _show_credit_or_library(self, path):
        icon = yield run_blocking(BioCartoonCLI.origin, path)
        if icon:
            self.show_bio_cartoon_credit(icon)
        else:
            self.toast("This picture has no embedded BioCartoon credit")
