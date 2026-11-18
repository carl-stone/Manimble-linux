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
from PySide6.QtCore import (
    QEvent, QLocale, QMimeData, QPersistentModelIndex, QPoint, QPointF, QRect, QRectF,
    QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QDrag, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit,
    QPushButton, QLabel, QComboBox, QCheckBox, QListWidget, QListWidgetItem,
    QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QMenu, QMessageBox,
    QGraphicsDropShadowEffect, QSizePolicy, QStyledItemDelegate, QStyle, QStyleOption, QToolButton, QToolTip,
)

from .icons import ASSETS, template_icon
from .models import frame_size, unit_px
from .style import Theme, watch_appearance
from .tasks import post, spawn, when_done
from .workers import run_blocking

DRAG_TYPE = "com.manimble.biocartoon-icon"
HTTP_HEADERS = {"User-Agent": "Manimble/BioCartoon"}
_BRAND_PNG = str(ASSETS / "biocartoon.png")


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
    def info_lines(self):
        lines = []
        if self.variant:
            lines.append("Variant: " + self.variant)
        if self.taxa:
            lines.append("Category: " + ", ".join(t.category + (" › " + t.subcategory if t.subcategory else "") for t in self.taxa))
        if self.category:
            lines.append("Database category: " + self.category)
        database = "BioCartoon" if self.database == "biocartoon" else self.source_name or self.source
        details = [database, self.license or "", "SVG" if self.svg_key is not None else "PNG"]
        if (self.width or 0) > 0 and (self.height or 0) > 0:
            details.append(f"{int(self.width)} × {int(self.height)} px")
        lines.append("  ·  ".join(x for x in details if x))
        return lines

    @property
    def info(self):
        lines = [self.title, *self.info_lines]
        if self.credit:
            lines.append(self.credit)
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

    def license_counts(self):
        return self.sql("SELECT license, count(DISTINCT coalesce(grp, source || '/' || title)) "
                        "FROM assets WHERE png_key IS NOT NULL GROUP BY license ORDER BY license")

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

    def _notify(self, future, quiet=False):
        def complete(f):
            try:
                f.result()
            except Exception as exc:
                if self.on_problem and not quiet:
                    self.on_problem(str(exc))
            if self.on_change:
                self.on_change()
        when_done(future, complete)
        return future

    def toggle(self, icon):
        return self._notify(run_blocking(self.toggle_sync, icon))

    def sync(self, force=False, *, quiet=False):
        return self._notify(run_blocking(self.sync_sync, force), quiet)


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
                loaded = QImage.fromData(f.result())
                if loaded.isNull():
                    raise ValueError("This icon has no preview")
                image = loaded
                self.cache[icon.id] = image
                while len(self.cache) > self.max_images:
                    self.cache.popitem(last=False)
            except Exception as exc:
                error = str(exc)
            for cb in self.waiting.pop(icon.id, []):
                cb(image, error)
        when_done(run_blocking(self._read, icon), completed)


class BCMultiPopup(QToolButton):
    def __init__(self, title):
        super().__init__()
        self.picked = set()
        self.names = {}
        self.on_change = None
        self.setAccessibleName(title)
        self.setFont(Theme.font(11))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setMenu(QMenu(self))

    def set_choices(self, choices, keys):
        self.menu().clear()
        self.names = {key: name for key, name, _ in choices}
        self.picked = keys & self.names.keys()
        for key, name, count in choices:
            row = self.menu().addAction(name if count is None else f"{name} ({_count(count)})")
            row.setCheckable(True)
            row.setChecked(key in self.picked)
            row.triggered.connect(lambda on, k=key: self._toggle(k, on))
        self._refresh_title()

    def _toggle(self, key, on):
        (self.picked.add if on else self.picked.discard)(key)
        self._refresh_title()
        if self.on_change:
            post(self.on_change)

    def _refresh_title(self):
        self.setText(", ".join(name for key, name in self.names.items() if key in self.picked) or "Any")


def _count(n):
    return QLocale().toString(int(n))


def _file_size(n):
    return QLocale().formattedDataSize(n, 1, QLocale.DataSizeFormat.DataSizeSIFormat)


def _label(text, size, weight=QFont.Weight.Normal):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setFont(Theme.font(size, weight))
    return label


def _set_ink(widget, color):
    palette = widget.palette()
    palette.setColor(widget.foregroundRole(), color)
    widget.setPalette(palette)


class BCInfoTip(QWidget):
    """The nonactivating card beside a hovered tile."""
    def __init__(self, parent):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(7, 7, 7, 10)
        self.card = QFrame()
        self.card.setObjectName("bioCartoonTip")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setColor(QColor(0, 0, 0, 85))
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 3)
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        self.rows = QVBoxLayout(self.card)
        self.rows.setContentsMargins(10, 10, 10, 10)
        self.rows.setSpacing(3)
        watch_appearance(self._appearance)
        self._appearance()

    def _appearance(self):
        self.card.setStyleSheet(
            f"QFrame#bioCartoonTip {{ background: {Theme.code_background().name()}; "
            f"border: 1px solid {Theme.separator().name(QColor.NameFormat.HexArgb)}; border-radius: 7px; }}")
        for i in range(self.rows.count()):
            if label := self.rows.itemAt(i).widget():
                _set_ink(label, Theme.secondary_ink() if label.property("secondary") else Theme.ink(1))

    def show_beside(self, icon, rect, error=None):
        while self.rows.count():
            if widget := self.rows.takeAt(0).widget():
                widget.hide()
                widget.deleteLater()
        labels = [_label(icon.title, 13, QFont.Weight.DemiBold)]
        labels.extend(_label(line, 12) for line in icon.info_lines)
        metadata = labels[-1]
        for line in (icon.credit, f"Preview error: {error}" if error else None):
            if line:
                label = _label(line, 10)
                label.setProperty("secondary", True)
                labels.append(label)
        screen = QApplication.screenAt(rect.center()) or self.screen()
        available = screen.availableGeometry()
        width = min(420, available.width() - 34, max(label.sizeHint().width() for label in labels))
        for label in labels:
            label.setWordWrap(True)
            label.setFixedWidth(width)
            self.rows.addWidget(label)
            if label is metadata and label is not labels[-1]:
                self.rows.addSpacing(2)
        self._appearance()
        self.adjustSize()
        x = rect.left() - self.width() - 1
        if x < available.left():
            x = rect.right() + 1
        x = max(available.left(), min(x, available.right() + 1 - self.width()))
        y = max(available.top(), min(rect.top() - 7, available.bottom() + 1 - self.height()))
        self.move(x, y)
        self.show()


class _ResultLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setFont(Theme.font(11))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(Theme.secondary_ink())
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)


_BADGES = {source: QColor(color) for source, color in (
    ("bioart", "#2F80ED"), ("bioicons", "#27AE60"), ("servier", "#8E44AD"),
    ("scidraw", "#E67E22"), ("healthicons", "#16A085"), ("reactome", "#C0392B"),
    ("openmoji", "#F1C40F"), ("user", "#607D8B"),
)}
_BADGES["biocartoon"] = Theme.warm
_BADGE_DEFAULT = QColor("#9E9E9E")


class _TilePainter(QStyledItemDelegate):
    def __init__(self, grid):
        super().__init__(grid)
        self.grid = grid
        self.star = template_icon("star", grid, tint=QColor("#808080"))
        self.starred = template_icon("star.fill", grid, tint=QColor("#E9AF00"))

    def sizeHint(self, option, index):
        return self.grid.gridSize()

    def card(self, rect):
        return QRect(rect.topLeft(), QSize(self.grid.side, self.grid.side))

    def star_rect(self, rect, icon):
        card = self.card(rect)
        count = self.grid.stars.count(icon)
        width = self.grid.fontMetrics().horizontalAdvance(BCStars.format(count)) if count > 0 else 0
        return QRect(card.right() - 4 - 12 - width - 4, card.top() + 3, 16 + width, 14)

    def paint(self, painter, option, index):
        icon = index.data(Qt.ItemDataRole.UserRole)
        if icon is None:
            return
        grid = self.grid
        rect = self.card(option.rect)
        focused = grid.keyboard and bool(option.state & QStyle.StateFlag.State_HasFocus)
        highlight = focused or grid.hovered == index
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setBrush(Qt.GlobalColor.white)
        painter.setPen(QPen(Theme.accent if highlight else Theme.separator(), 2 if highlight else 1))
        painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 8, 8)
        image = grid.image(icon)
        if image is not None and not image.isNull():
            fitted = image.size()
            fitted.scale(rect.size() - QSize(16, 16), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(QPoint(), fitted)
            target.moveCenter(rect.center())
            painter.drawImage(target, image)
        painter.setFont(grid.font())
        code = grid.code(icon.database)
        if code:
            badge = QRect(rect.left() + 5, rect.bottom() - 18, 22, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_BADGES.get(icon.database, _BADGE_DEFAULT))
            painter.drawRoundedRect(QRectF(badge), 4, 4)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, code)
        star = self.star_rect(option.rect, icon)
        symbol = self.starred if grid.stars.is_starred(icon) else self.star
        symbol.paint(painter, QRect(star.right() - 9, star.top() + 2, 10, 10))
        count = grid.stars.count(icon)
        if count > 0:
            painter.setPen(QColor("#555555"))
            painter.drawText(star.adjusted(0, 0, -12, 0),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, BCStars.format(count))
        painter.restore()


class BCTile(QListWidget):
    """Scrollable icon grid and drag source; each item retains an accessible title."""
    insertRequested = Signal(object)
    starRequested = Signal(object)
    downloadRequested = Signal(object)

    def __init__(self, thumbs, stars, code):
        super().__init__()
        self.thumbs, self.stars, self.code = thumbs, stars, code
        self.setAccessibleName("BioCartoon icons")
        self.setAccessibleDescription("Arrow keys select an icon; Return inserts it, Space shows details, and Shift+F10 opens its menu.")
        self.setFont(Theme.font(9, QFont.Weight.DemiBold))
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWrapping(True)
        self.side = 96
        self.setGridSize(QSize(102, 102))
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportMargins(0, 2, 0, 12)
        self.setStyleSheet("QListWidget { background: transparent; }")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._painter = _TilePainter(self)
        self.setItemDelegate(self._painter)
        self.hovered = QPersistentModelIndex()
        self._pressed = QPersistentModelIndex()
        self._star_pressed = False
        self._start = QPoint()
        self.keyboard = False
        self.errors, self.requested = {}, set()
        self.tip = BCInfoTip(self)
        self.tip_delay = QTimer(self, singleShot=True, interval=500)
        self.tip_delay.timeout.connect(self._show_tip)
        self.verticalScrollBar().valueChanged.connect(self.clear_hover)
        self.viewport().installEventFilter(self)

    def clear_results(self):
        self.clear_hover()
        self.errors.clear()
        self.clear()

    def image(self, icon):
        image = self.thumbs.cache.get(icon.id)
        if image is not None:
            self.thumbs.cache.move_to_end(icon.id)
        elif icon.id not in self.requested and icon.id not in self.errors:
            self.requested.add(icon.id)
            post(lambda: self._request_image(icon))
        return image

    def _request_image(self, icon):
        if not self.isVisible():
            self.requested.discard(icon.id)
            return
        def done(image, error):
            self.requested.discard(icon.id)
            if error:
                self.errors[icon.id] = error
            if self.isVisible():
                self.viewport().update()
                if error and self.tip.isVisible() and self.hovered.data(Qt.ItemDataRole.UserRole) == icon:
                    self._show_tip()
        self.thumbs.image(icon, done)

    def _fit_grid(self):
        gap = 6
        width = self.maximumViewportSize().width()
        style = self.style()
        bar = self.verticalScrollBar()
        metric = QStyle.PixelMetric
        # IconMode reserves space for a non-overlay scrollbar even while it is hidden.
        if not style.pixelMetric(metric.PM_ScrollView_ScrollBarOverlap, None, bar):
            width -= style.pixelMetric(metric.PM_ScrollBarExtent, None, bar)
            if style.styleHint(QStyle.StyleHint.SH_ScrollView_FrameOnlyAroundContents, None, self):
                option = QStyleOption()
                option.initFrom(self)
                width -= 2 * style.pixelMetric(metric.PM_DefaultFrameWidth, option, self)
        columns = max(2, (width + gap) // (96 + gap))
        # Qt wraps at the right edge and counts the trailing gap as part of the last cell.
        self.side = max(40, (width - 1) // columns - gap)
        cell = QSize(self.side + gap, self.side + gap)
        if cell != self.gridSize():
            self.setGridSize(cell)

    def eventFilter(self, obj, event):
        if obj is self.viewport():
            if event.type() == QEvent.Type.Leave:
                self.clear_hover()
            elif event.type() == QEvent.Type.Resize:
                self._fit_grid()
            elif event.type() == QEvent.Type.ToolTip:
                index = self.indexAt(event.pos())
                icon = index.data(Qt.ItemDataRole.UserRole)
                if icon and self._painter.star_rect(self.visualRect(index), icon).contains(event.pos()):
                    self.tip.hide()
                    self.tip_delay.stop()
                    tip = ("Starred: click to take your star back" if self.stars.is_starred(icon)
                           else "Star this icon (shared with the BioCartoon editor)")
                    QToolTip.showText(event.globalPos(), tip, self.viewport())
                return True
        return super().eventFilter(obj, event)

    def clear_hover(self, *_):
        self.tip_delay.stop()
        self.tip.hide()
        QToolTip.hideText()
        if self.hovered.isValid():
            self.hovered = QPersistentModelIndex()
            self.viewport().update()

    def _show_tip(self):
        if self.hovered.isValid() and self.isVisible() and self.window().isActiveWindow():
            index = self.model().index(self.hovered.row(), 0)
            icon = index.data(Qt.ItemDataRole.UserRole)
            rect = self._painter.card(self.visualRect(index))
            rect.moveTopLeft(self.viewport().mapToGlobal(rect.topLeft()))
            self.tip.show_beside(icon, rect, self.errors.get(icon.id))

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._pressed.isValid() and event.buttons() & Qt.MouseButton.LeftButton and not self._star_pressed:
            delta = point - self._start
            if delta.x() ** 2 + delta.y() ** 2 > 16:
                icon = self._pressed.data(Qt.ItemDataRole.UserRole)
                self._pressed = QPersistentModelIndex()
                self._drag(icon)
                self.setState(QAbstractItemView.State.NoState)
                return
        index = self.indexAt(point)
        if index.isValid() and not self._painter.card(self.visualRect(index)).contains(point):
            index = self.model().index(-1, 0)
        if self.hovered != index:
            self.clear_hover()
            self.hovered = QPersistentModelIndex(index)
            if index.isValid() and not event.buttons():
                self.tip_delay.start()
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self.clear_hover()
        self.keyboard = False
        self._pressed = QPersistentModelIndex()
        self._star_pressed = False
        if event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            index = self.indexAt(point)
            if index.isValid() and self._painter.card(self.visualRect(index)).contains(point):
                self._pressed = QPersistentModelIndex(index)
                self._start = point
                self._star_pressed = self._painter.star_rect(
                    self.visualRect(index), index.data(Qt.ItemDataRole.UserRole)).contains(point)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # The first release has already inserted it.
        event.accept()

    def mouseReleaseEvent(self, event):
        pressed, star = self._pressed, self._star_pressed
        self._pressed = QPersistentModelIndex()
        self._star_pressed = False
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or not pressed.isValid():
            return
        point = event.position().toPoint()
        index = self.indexAt(point)
        if pressed != index or not self._painter.card(self.visualRect(index)).contains(point):
            return
        icon = index.data(Qt.ItemDataRole.UserRole)
        if star:
            if self._painter.star_rect(self.visualRect(index), icon).contains(point):
                self.starRequested.emit(icon)
        else:
            self.insertRequested.emit(icon)

    def keyPressEvent(self, event):
        self.clear_hover()
        self.keyboard = True
        index = self.currentIndex()
        icon = index.data(Qt.ItemDataRole.UserRole)
        if icon:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.insertRequested.emit(icon)
                return
            if key == Qt.Key.Key_Space:
                self._details(icon)
                return
            if key == Qt.Key.Key_Menu or (key == Qt.Key.Key_F10 and event.modifiers() == Qt.KeyboardModifier.ShiftModifier):
                self._menu(icon, self.viewport().mapToGlobal(self.visualRect(index).center()))
                return
        super().keyPressEvent(event)
        self.viewport().update()

    def contextMenuEvent(self, event):
        index = self.indexAt(self.viewport().mapFromGlobal(event.globalPos()))
        if index.isValid():
            self._menu(index.data(Qt.ItemDataRole.UserRole), event.globalPos())

    def _menu(self, icon, pos):
        self.clear_hover()
        menu = QMenu(self)
        menu.addAction("Insert on Slide", lambda: self.insertRequested.emit(icon))
        menu.addAction("Unstar" if self.stars.is_starred(icon) else "Star", lambda: self.starRequested.emit(icon))
        saved = self.thumbs.offline.saved(icon)
        menu.addAction("Show Download Folder" if saved else "Download", lambda: self.downloadRequested.emit(icon))
        menu.addSeparator()
        menu.addAction("Copy Name", lambda: QApplication.clipboard().setText(icon.title))
        if icon.credit:
            menu.addAction("Copy Credit", lambda: QApplication.clipboard().setText(icon.credit))
        menu.exec(pos)
        menu.deleteLater()

    def _details(self, icon):
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(icon.title)
        layout = QVBoxLayout(dialog)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(preview)
        info = _label(icon.info, 12)
        info.setWordWrap(True)
        info.setMaximumWidth(420)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        layout.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if icon.credit:
            copy = buttons.addButton("Copy Credit", QDialogButtonBox.ButtonRole.ActionRole)
            copy.clicked.connect(lambda: QApplication.clipboard().setText(icon.credit))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        active = [True]
        dialog.finished.connect(lambda: active.clear())
        def done(image, error):
            if not active:
                return
            if image is not None and not image.isNull():
                preview.setPixmap(QPixmap.fromImage(image))
            elif error:
                info.setText(icon.info + "\nPreview error: " + error)
            dialog.adjustSize()
        dialog.open()
        self.thumbs.image(icon, done)

    def _drag(self, icon):
        self.clear_hover()
        mime = QMimeData()
        mime.setData(DRAG_TYPE, json.dumps(icon.to_dict(), ensure_ascii=False).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        dpr = self.devicePixelRatioF()
        image = self.thumbs.cache.get(icon.id)
        preview = QPixmap.fromImage(image) if image is not None else QPixmap(_BRAND_PNG)
        side = round(96 * dpr)
        preview = preview.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        preview.setDevicePixelRatio(dpr)
        drag.setPixmap(preview)
        drag.setHotSpot(QPoint(round(preview.width() / (2 * dpr)), round(preview.height() / (2 * dpr))))
        drag.exec(Qt.DropAction.CopyAction)


class _BioCartoonMark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._icon = template_icon("biocartoon", self)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        self._icon.paint(painter, self.rect(), Qt.AlignmentFlag.AlignCenter, mode)


class BioCartoonPane(QWidget):
    drag_type = DRAG_TYPE
    _download_tip = "Download every icon's image, so searches show them without internet"
    _sorts = (("Relevance", "relevance"), ("Popular", "popular"), ("A–Z", "az"), ("Z–A", "za"))

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
        self.generation = 0
        self.is_loading = self._more = self._locating = False
        self._batch = None
        self.facets = None
        self.facets_settled = False
        self.sources, self.categories, self.source_categories, self.licenses = {}, [], [], []
        self._sort_picked = False
        self.page_size = 60
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(7)
        header.addWidget(_BioCartoonMark(self))
        header.addWidget(_label("BioCartoon", 14, QFont.Weight.DemiBold))
        header.addStretch()
        self.bulk = QPushButton("Download")
        self.bulk.setFont(Theme.font(11))
        self.bulk.setToolTip(self._download_tip)
        self.bulk.clicked.connect(self._bulk_pressed)
        header.addWidget(self.bulk)
        self.close_button = self._icon_button("Close", "Back to the inspector", 20)
        self.close_button.clicked.connect(lambda: self.on_close() if self.on_close else self.hide())
        header.addWidget(self.close_button)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        search_row.setSpacing(2)
        self.search_field = QLineEdit()
        self.search_field.setFont(Theme.font(13))
        self.search_field.setPlaceholderText("Search icons (cell, neuron, mouse…)")
        self.search_field.setAccessibleName("Search icons")
        self.search_field.setClearButtonEnabled(True)
        search_row.addWidget(self.search_field, 1)
        self.funnel = self._icon_button("Filters", "", 24)
        self.funnel.setCheckable(True)
        self.funnel.toggled.connect(self._filter_disclosure)
        search_row.addWidget(self.funnel)
        layout.addLayout(search_row)
        self.summary = _label("", 10)
        self.summary.setWordWrap(True)
        self.summary.setMaximumHeight(self.summary.fontMetrics().lineSpacing() * 2 + 2)
        layout.addWidget(self.summary)

        self.typing = QTimer(self, singleShot=True, interval=300)
        self.typing.timeout.connect(self._filters_changed)
        self.search_field.textChanged.connect(self._start_typing)
        self.search_field.returnPressed.connect(self._filters_changed)
        self.filter_box = self._build_filters()
        self.filter_box.hide()
        layout.addWidget(self.filter_box)

        sort_row = QHBoxLayout()
        sort_row.setSpacing(6)
        self.sort = QComboBox()
        self.sort.setFont(Theme.font(11))
        self.sort.activated.connect(self._sort_changed)
        sort_caption = _label("Sort", 11)
        sort_caption.setBuddy(self.sort)
        sort_row.addWidget(sort_caption)
        sort_row.addWidget(self.sort)
        self.status = _ResultLabel()
        sort_row.addWidget(self.status, 1)
        # The Linux catalog/usage refresh is not an offline image download.
        self.sync = QPushButton("Sync")
        self.sync.setFont(Theme.font(11))
        self.sync.setToolTip("Check for catalog updates and sync stars")
        self.sync.setEnabled(False)
        self.sync.clicked.connect(lambda: spawn(self._sync()))
        sort_row.addWidget(self.sync)
        layout.addLayout(sort_row)

        self.tiles = BCTile(self.thumbs, self.stars, self._code)
        self.tiles.insertRequested.connect(lambda icon: self.on_insert(icon) if self.on_insert else None)
        self.tiles.starRequested.connect(self.stars.toggle)
        self.tiles.downloadRequested.connect(lambda icon: spawn(self._download_one(icon)))
        self.tiles.verticalScrollBar().valueChanged.connect(self._load_near_bottom)
        layout.addWidget(self.tiles, 1)
        self.message = _label("", 10)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.message.setMaximumHeight(self.message.fontMetrics().lineSpacing() * 3 + 2)
        self.message.hide()
        layout.addWidget(self.message)
        QApplication.instance().applicationStateChanged.connect(self._application_state)
        watch_appearance(self._appearance)
        self._appearance()
        self._follow_search()
        self._choices()

    @staticmethod
    def _icon_button(name, tip, width):
        button = QToolButton()
        button.setAutoRaise(True)
        button.setFixedWidth(width)
        button.setIconSize(QSize(16, 16))
        button.setAccessibleName(name)
        button.setToolTip(tip)
        return button

    def _build_filters(self):
        box = QFrame()
        box.setObjectName("bioCartoonFilters")
        stack = QVBoxLayout(box)
        stack.setContentsMargins(10, 8, 10, 8)
        stack.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.popups = {}
        self.filter_labels = []
        self.authors, self.tags = QLineEdit(), QLineEdit()
        for field in (self.authors, self.tags):
            field.setFont(Theme.font(11))
            field.setPlaceholderText("Any (commas for several)")
            field.textChanged.connect(self._start_typing)
            field.returnPressed.connect(self._filters_changed)
        rows = (
            ("sources", "Database", None, ""), ("", "Author", self.authors, ""),
            ("categories", "Category", None, ""), ("subcategories", "Subcategory", None, ""),
            ("db_cats", "Database category", None, "A database's own categories, by its code (BA: NIAID NIH BioArt, …)"),
            ("licenses", "License", None, ""), ("file_types", "File type", None, ""),
            ("", "Tags", self.tags, "Icons with one of these as a whole tag, any case"),
        )
        for row, (key, name, field, tip) in enumerate(rows):
            caption = _label(name, 11)
            caption.setFixedWidth(104)
            caption.setToolTip(tip)
            self.filter_labels.append(caption)
            if field is None:
                field = self.popups[key] = BCMultiPopup(name)
                field.on_change = self._filters_changed
            if tip:
                field.setToolTip(tip)
            caption.setBuddy(field)
            grid.addWidget(caption, row, 0)
            grid.addWidget(field, row, 1)
        self.favorites, self.downloads = QCheckBox("Favorites"), QCheckBox("Downloads")
        self.favorites.setToolTip("Only icons you starred")
        self.downloads.setToolTip("Only icons you placed on a slide (or downloaded in the BioCartoon editor)")
        for row, cb in enumerate((self.favorites, self.downloads), len(rows)):
            cb.setFont(Theme.font(11))
            cb.toggled.connect(self._filters_changed)
            grid.addWidget(cb, row, 1)
        stack.addLayout(grid)
        self.clear_button = QPushButton("Clear filters")
        self.clear_button.setFont(Theme.font(11))
        self.clear_button.clicked.connect(lambda: self.apply(BCFilter()))
        stack.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignLeft)
        return box

    def _appearance(self):
        self.filter_box.setStyleSheet("QFrame#bioCartoonFilters { "
                                      f"background: {Theme.ink(.09).name(QColor.NameFormat.HexArgb)}; border-radius: 8px; }}")
        self.close_button.setIcon(template_icon("xmark.circle.fill", self.close_button, tint=Theme.tertiary_ink()))
        for label in (self.summary, self.message, *self.filter_labels):
            _set_ink(label, Theme.secondary_ink())
        self._filter_chrome()
        self.tiles.viewport().update()

    def hideEvent(self, event):
        self.tiles.clear_hover()
        super().hideEvent(event)

    def _application_state(self, state):
        if state != Qt.ApplicationState.ApplicationActive:
            self.tiles.clear_hover()

    @property
    def result_count(self):
        return len(self.all_results)

    @property
    def first_result(self):
        return next(iter(self.all_results), None)

    def _problem(self, text):
        self.status.setText(str(text))

    def _set_message(self, text, *, clear=False):
        if clear:
            self.generation += 1
            self.is_loading = self._more = False
            self.facets_settled = True
            self.all_results.clear()
            self.tiles.clear_results()
            self.status.clear()
        self.message.setText(str(text))
        self.message.setToolTip(str(text))
        self.message.setVisible(bool(text))

    def activate(self, within=None):
        self.search_field.setFocus()
        self._sort_picked = False
        if self.exe:
            if self._follow_search():
                self.reload()
            self._sync_stars()
        elif not self._locating:
            self._locating = True
            spawn(self._activate())

    def _activate(self):
        self._set_message("")
        self.status.setText("Looking for biocartoon…")
        try:
            yield self.cli.usable()
            yield from self._catalog_choices()
            yield run_blocking(BCDownloads.import_editor)
            yield run_blocking(self.stars.load_sync)
            self.exe = self.cli.exe
            self.sync.setEnabled(True)
            self.reload()
            self._sync_stars()
        except Exception as exc:
            self._set_message(exc, clear=True)
        finally:
            self._locating = False

    def _sync_stars(self):
        before = self.stars.counts
        def done(future):
            if (self.isVisible() and not self.filter.favorites and self.sort.currentData() == "popular"
                    and not future.cancelled() and future.exception() is None and self.stars.counts != before):
                self.reload()
        when_done(self.stars.sync(quiet=True), done)

    def _catalog_choices(self):
        self.sources = {src.source: src for src in (yield self.cli.sources())}
        for key, load in (("categories", self.cli.categories), ("source_categories", self.cli.source_categories),
                          ("licenses", self.cli.license_counts)):
            try:
                setattr(self, key, (yield load()))
            except Exception as exc:
                self._set_message(exc)
        self.facets = None
        self._choices()

    def _sync(self):
        if not self.exe or not self.sync.isEnabled():
            return
        self.sync.setEnabled(False)
        try:
            self.status.setText("Updating catalog…")
            yield self.cli.sync()
            yield from self._catalog_choices()
            self.stars.sync(True)
            self.reload()
        except Exception as exc:
            self._set_message(exc)
        finally:
            self.sync.setEnabled(True)

    def type(self, text):
        self.search_field.setText(text)

    def apply(self, f):
        self.filter = f
        self._choices()
        for field, value in ((self.authors, f.authors), (self.tags, f.tags),
                             (self.favorites, f.favorites), (self.downloads, f.downloads)):
            field.blockSignals(True)
            if isinstance(field, QLineEdit):
                field.setText(value)
            else:
                field.setChecked(value)
            field.blockSignals(False)
        self.funnel.setChecked(True)
        self._filter_chrome()
        self.reload()

    def _start_typing(self):
        self.typing.start()

    def _filters_changed(self):
        prior = self.filter
        self.filter = BCFilter(**{k: p.picked.copy() for k, p in self.popups.items()},
                               authors=self.authors.text(), tags=self.tags.text(),
                               favorites=self.favorites.isChecked(), downloads=self.downloads.isChecked())
        if self.filter.sources != prior.sources or self.filter.categories != prior.categories:
            self._choices()
            self.filter = replace(self.filter, subcategories=self.popups["subcategories"].picked.copy(),
                                  db_cats=self.popups["db_cats"].picked.copy())
        self._filter_chrome()
        self.reload()

    def _filter_disclosure(self, opened):
        self.filter_box.setVisible(opened)
        self._filter_chrome()

    def _filter_chrome(self):
        active, opened = not self.filter.is_empty, self.funnel.isChecked()
        tint = Theme.accent if active or opened else Theme.secondary_ink()
        self.funnel.setIcon(template_icon("line.3.horizontal.decrease.circle", self.funnel, tint=tint))
        self.funnel.setToolTip("Hide the filters" if opened else
                               "Filter by database, author, category, license, file type, tags, favorites or downloads")
        self.clear_button.setVisible(active)
        text = self._summary_text()
        self.summary.setText(text)
        self.summary.setToolTip(text)
        self.summary.setVisible(active and not opened)

    @staticmethod
    def _source_name(source):
        return "Community and user-generated" if source.source == "user" else source.name

    def _code(self, source):
        known = self.sources.get(source)
        return known.code if known and known.code is not None else source[:2].upper()

    def _summary_text(self):
        f = self.filter
        names = [self._source_name(s) for s in self.sources.values() if s.source in f.sources]
        if BCFilter.library in f.sources:
            names.append("Your library")
        authors = [p.strip() for p in f.authors.split(",") if p.strip()]
        taxa = {c.slug: c.name for c in self.categories}
        groups = [", ".join(names), "by " + ", ".join(authors) if authors else "",
                  ", ".join(taxa.get(k, k) for k in sorted(f.categories) + sorted(f.subcategories)),
                  ", ".join(f"{self._code(db)}: {cat}" for db, cat in map(BCFilter.split, sorted(f.db_cats))),
                  ", ".join(sorted(f.licenses)), ", ".join(t.upper() for t in sorted(f.file_types)),
                  "tagged " + ", ".join(f.tag_list) if f.tag_list else "",
                  "Favorites" if f.favorites else "", "Downloads" if f.downloads else ""]
        return "  ·  ".join(group for group in groups if group)

    def _sort_changed(self):
        self._sort_picked = True
        self._filters_changed()

    def _follow_search(self):
        query = self.search_field.text().strip()
        previous = self.sort.currentData()
        choices = self._sorts if query else self._sorts[1:]
        if self.sort.count() != len(choices):
            self.sort.clear()
            for title, key in choices:
                self.sort.addItem(title, key)
        automatic = "relevance" if query else "popular"
        key = previous if self._sort_picked and self.sort.findData(previous) >= 0 else automatic
        self.sort.setCurrentIndex(self.sort.findData(key))
        return previous != key

    def reload(self):
        self.typing.stop()
        self._follow_search()
        if not self.exe:
            return
        self._set_message("")
        self.generation += 1
        self.is_loading = self._more = self.facets_settled = False
        self._query = (self.filter, self.search_field.text().strip(), self.sort.currentData())
        self.all_results.clear()
        self.tiles.clear_results()
        self.tiles.verticalScrollBar().setValue(0)
        self._load_page()

    def _load_near_bottom(self, value):
        if self._more and self.isVisible() and value >= self.tiles.verticalScrollBar().maximum() - 3 * 102:
            self._load_page()

    def _load_page(self):
        if self.is_loading:
            return
        self.is_loading = True
        offset = len(self.all_results)
        self.status.setText("Loading more…" if offset else "Searching…")
        spawn(self._load_task(self.generation, *self._query, offset))

    def _load_task(self, generation, f, query, sort, offset):
        try:
            rows = yield self.cli.search(query, f, sort, offset, self.page_size)
            if generation != self.generation:
                return
            self._more = len(rows) == self.page_size
            self.all_results.extend(rows)
            for icon in rows:
                item = QListWidgetItem(icon.title)
                item.setData(Qt.ItemDataRole.UserRole, icon)
                self.tiles.addItem(item)
            n = len(self.all_results)
            self.status.setText("No results" if n == 0 else "1 icon" if n == 1 and not self._more else
                                f"{n}{'+' if self._more else ''} icons")
        except Exception as exc:
            if generation == self.generation:
                self._set_message(exc, clear=True)
            return
        finally:
            if generation == self.generation:
                self.is_loading = False
        if offset == 0:
            try:
                fc = yield self.cli.facets(query, f)
                if generation == self.generation:
                    self.facets = fc
                    self._choices()
                    self._filter_chrome()
            except Exception as exc:
                if generation == self.generation:
                    self._set_message(exc)
            finally:
                if generation == self.generation:
                    self.facets_settled = True

    def _choices(self):
        f, fc = self.filter, self.facets

        def set_choices(key, choices):
            popup = self.popups[key]
            picks = getattr(f, key)
            popup.set_choices([(k, name, n) for k, name, n in choices if n is None or n > 0 or k in picks], picks)

        sources = [(s.source, self._source_name(s), fc.source(s.source) if fc else s.icons)
                   for s in self.sources.values() if s.icons]
        if fc is not None or BCFilter.library in f.sources:
            sources.append((BCFilter.library, "Your library", fc.source(BCFilter.library) if fc else 0))
        set_choices("sources", sources)

        tops = {c.slug: c.name for c in self.categories if "." not in c.slug}
        categories, subs = [], []
        for c in self.categories:
            parent, sep, _ = c.slug.partition(".")
            n = fc.taxon(c.slug) if fc else c.icons
            if not sep:
                categories.append((c.slug, c.name, n))
            elif not f.categories or parent in f.categories:
                name = c.name if len(f.categories) == 1 else f"{tops.get(parent, parent)}: {c.name}"
                subs.append((c.slug, name, n))
        set_choices("categories", categories)
        set_choices("subcategories", subs)

        order = {source: n for n, source in enumerate(self.sources)}
        db_counts = {BCFilter.key(r["source"], r["category"]): r.get("icons", 0)
                     for r in (fc.source_categories if fc else self.source_categories) if r.get("source") and r.get("category")}
        db_counts.update({key: 0 for key in f.db_cats - db_counts.keys()})
        dbs = [(db, category, key, n) for key, n in db_counts.items() for db, category in (BCFilter.split(key),)
               if not f.sources or db in f.sources or BCFilter.library in f.sources]
        dbs.sort(key=lambda row: (order.get(row[0], len(order)), row[1].casefold()))
        set_choices("db_cats", [(key, f"{self._code(db)}: {category}", n) for db, category, key, n in dbs])

        licenses = ({r["license"]: r["icons"] for r in fc.licenses if r.get("license")} if fc else
                    {key: n for key, n in self.licenses if key})
        licenses.update({key: 0 for key in f.licenses - licenses.keys()})
        set_choices("licenses", [(key, key, licenses[key]) for key in sorted(licenses)])
        types = {r["file_type"].lower(): r["icons"] for r in fc.file_types if r.get("file_type")} if fc else {}
        set_choices("file_types", [(k, k.upper(), types.get(k, 0) if fc else None) for k in ("svg", "png")])

    def _stars_changed(self):
        self.tiles.viewport().update()
        if self.filter.favorites:
            self.reload()

    def _download_one(self, icon):
        self.status.setText("Downloading " + icon.title + "…")
        try:
            file = yield run_blocking(self.offline.save, icon)
            yield BCDownloads.add(icon)
            if self.filter.downloads:
                self.reload()
            self.status.setText("Downloaded " + icon.title)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(file.parent)))
        except Exception as exc:
            self._set_message(f"Could not download {icon.title}: {exc}", clear=True)

    def _bulk_pressed(self):
        if self._batch:
            self._batch.cancel()
        elif not self.exe:
            self._set_message("BioCartoon isn't ready yet: install it first.", clear=True)
        else:
            self.bulk.setEnabled(False)
            self.bulk.setText("Checking…")
            spawn(self._bulk())

    def _bulk(self):
        batch = BCOffline()
        try:
            try:
                missing = yield run_blocking(batch.missing_sync, self.cli)
            except Exception as exc:
                self._set_message(f"Could not list the catalog's images: {exc}", clear=True)
                return
            folder = batch.locate()
            keys, total = missing["keys"], missing["total"]
            n = len(keys)
            if not n:
                self._set_message(f"All {_count(total)} images are downloaded, in {folder}.", clear=True)
                if folder.is_dir():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
                return
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(f"Download {_count(n)} BioCartoon images?")
            body = f"About {_file_size(missing['bytes'])}, into {folder} (where the BioCartoon editor keeps its offline images). "
            if n < total:
                body += f"The other {_count(total - n)} are there already. "
            box.setInformativeText(body + "Searches then show them without internet.")
            download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
            cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(cancel)
            box.setEscapeButton(cancel)
            box.exec()
            if box.clickedButton() != download:
                return

            self._batch = batch
            self.bulk.setText("Stop")
            self.bulk.setToolTip("Stop downloading (images downloaded so far are kept)")
            self.bulk.setEnabled(True)
            self.status.setText(f"Downloading {_count(n)} images…")

            def show_progress(done):
                if self._batch is batch:
                    self.bulk.setText(f"Stop · {done * 100 // n}%")
                    self.status.setText(f"Downloaded {_count(done)} of {_count(n)} images…")

            result = yield run_blocking(batch.download, keys, lambda got, failed: post(lambda: show_progress(got + failed)))
            yield run_blocking(batch.record_downloaded_sync, self.cli)
            stopped, failed = result["cancelled"], result["failed"]
            text = ("Download stopped: " if stopped else "Downloaded ") + f"{_count(result['got'])} images into {folder}"
            detail = f"; {_count(failed)} could not be downloaded (press Download to try them again)." if failed and not stopped else "."
            self._set_message(text + detail, clear=True)
            if folder.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception as exc:
            self._set_message(exc, clear=True)
        finally:
            self._batch = None
            self.bulk.setEnabled(True)
            self.bulk.setText("Download")
            self.bulk.setToolTip(self._download_tip)


class BioCartoonMixin:
    def show_bio_cartoon(self):
        pane = getattr(self, "biocartoon_pane", None)
        if pane is not None and self.inspector_host.currentWidget() is pane and pane.isVisible():
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
        pane = getattr(self, "biocartoon_pane", None)
        if pane is not None and self.inspector_host.currentWidget() is pane:
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
            fetching = True
            show_download = not (self.busy or self.verifying)
            if show_download:
                self.busy_pill.show(f"Downloading {icon.title}…", spinning=True, cancellable=False)
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
                fetching = False
                if show_download:
                    self.update_busy_pill()
                half = 1.5 * unit * min(1, dims[0] / max(dims[1], 1))
                if self.work_deck != deck:
                    raise RuntimeError("The deck changed while the icon was downloading; nothing was inserted")
                req = {"scene": context.scene, "stop": context.stop, "seen": context.seen,
                       "items": [{"kind": "image", "file": dest.name, "path": str(dest),
                                  "sx": point.x() - half, "sy": point.y(), "ex": point.x() + half, "ey": point.y()}]}
                reply = yield self.call("insert_many", req)
                if not reply.get("ok"):
                    raise RuntimeError(reply.get("error") or "insert failed")
                yield BCDownloads.add(icon)
                yield from self.reload_state()
                if self.scene_name == context.scene:
                    yield from self.load_scene(context.scene, True)
                self.toast("Inserted image")
            except Exception as exc:
                if fetching and show_download:
                    self.update_busy_pill()
                self.toast(f"Could not download {icon.title}: {exc}" if fetching else str(exc), error=True)
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
        credit = icon.credit if icon.credit is not None else icon.title
        alert = QMessageBox(self.window)
        alert.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        alert.setTextFormat(Qt.TextFormat.PlainText)
        alert.setText(icon.title)
        alert.setInformativeText(credit)
        alert.setIconPixmap(QPixmap(_BRAND_PNG))
        copy = alert.addButton("Copy Credit", QMessageBox.ButtonRole.AcceptRole)
        done = alert.addButton("Done", QMessageBox.ButtonRole.RejectRole)
        alert.setDefaultButton(copy)
        alert.setEscapeButton(done)

        def clicked(button):
            if button == copy:
                QApplication.clipboard().setText(credit)
                self.toast("Copied the credit")

        alert.buttonClicked.connect(clicked)
        alert.open()

    def bio_cartoon_menu_items(self, path):
        def read_origin():
            if not Path(path).is_file():
                return False, None
            return True, BioCartoonCLI.origin(path)

        present, icon = yield run_blocking(read_origin)
        if not present:
            return []
        if icon is not None:
            return [("Show Credit…", lambda: self.show_bio_cartoon_credit(icon), "")]
        return (yield from self.bio_cartoon_library_items(path))
