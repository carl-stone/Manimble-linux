"""BioCartoon's local user library and explicitly confirmed submissions.

The local asset is committed before publication is attempted. All database,
image and HTTP work is performed in the shared I/O pool, never in the GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageOps
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
    QLabel, QComboBox, QCheckBox, QDialogButtonBox, QMessageBox, QScrollArea, QWidget,
)

from . import prefs
from .biocartoon import BioCartoonCLI, BCIcon, atomic_bytes, biocartoon_data, HTTP_HEADERS
from .tasks import spawn
from .workers import run_blocking


class SVGRefused(RuntimeError):
    """A definitive server refusal; a new, disclosed PNG request may be offered."""
    def __init__(self, reason, reservation):
        super().__init__(reason)
        self.reservation = reservation


class BCLibrary:
    max_png_bytes = max_svg_bytes = 2 * 1024 * 1024
    max_image_side = 4096
    max_per_hour = 10
    licenses = [("CC0-1.0", "CC0 (no credit needed)"), ("CC-BY-4.0", "CC BY 4.0 (users must credit you)")]
    cc_by_needs_no_ai = "CC BY requires confirmation that no generative AI was used."
    already_submitted = "Icon has already been submitted"

    @dataclass
    class Picture:
        svg: str | None
        png: bytes
        size: tuple[int, int]
        bytes: int
        reduced_from: tuple[int, int] | None = None

    @dataclass
    class Row:
        title: str
        category: str = ""
        description: str = ""
        license: str = ""
        author: str = ""
        tags: list[str] | None = None
        ai_used: bool | None = None

    @dataclass
    class Meta:
        title: str
        description: str = ""
        category: str = ""
        tags: list[str] | None = None
        author: str = "anonymous"
        license: str = "CC0-1.0"
        generative_ai_used: bool | None = None
        email: str = ""
        official: bool = False

    @staticmethod
    def submit_url():
        return (os.environ.get("BIOCARTOON_SUBMIT_URL") or "https://api.biocartoon.com").rstrip("/")

    @staticmethod
    def is_svg(data):
        return b"<svg" in data[:4096].lower()

    @classmethod
    def fit_png(cls, image):
        original = image
        scale = 1.0
        while True:
            out = BytesIO()
            image.save(out, "PNG", optimize=True)
            data = out.getvalue()
            if len(data) <= cls.max_png_bytes:
                return image, data
            if max(image.size) <= 1:
                raise ValueError("The picture cannot fit the PNG limit")
            scale *= min(math.sqrt(cls.max_png_bytes / len(data)) * .98, .95)
            image = original.resize(tuple(max(1, round(x * scale)) for x in original.size), Image.Resampling.LANCZOS)

    @classmethod
    def read(cls, path):
        data = Path(path).read_bytes()
        if cls.is_svg(data):
            text = data.decode("utf-8")
            renderer = QSvgRenderer(QByteArray(data))
            if not renderer.isValid():
                raise ValueError("The SVG cannot be drawn")
            size = renderer.defaultSize()
            if size.width() <= 0 or size.height() <= 0:
                raise ValueError("The SVG has no usable dimensions")
            long = max(size.width(), size.height())
            scale = min(cls.max_image_side, max(1024, long)) / long
            image = QImage(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            renderer.render(painter)
            painter.end()
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG"):
                raise ValueError("The SVG preview cannot be saved")
            with Image.open(BytesIO(bytes(buffer.data()))) as src:
                fit, png = cls.fit_png(src.convert("RGBA"))
            return cls.Picture(text, png, fit.size, len(data))
        with Image.open(BytesIO(data)) as src:
            image = ImageOps.exif_transpose(src).convert("RGBA")
        own = image.size
        image.thumbnail((cls.max_image_side, cls.max_image_side), Image.Resampling.LANCZOS)
        fit, png = cls.fit_png(image)
        return cls.Picture(None, png, fit.size, len(png), own if own != fit.size else None)

    @staticmethod
    def fingerprint(path):
        h = 0xcbf29ce484222325
        with open(path, "rb") as stream:
            while chunk := stream.read(65536):
                for byte in chunk:
                    h = ((h ^ byte) * 0x100000001b3) & 0xffffffffffffffff
        return f"file:{h:016x}"

    @staticmethod
    def check_email(value):
        value = value.strip()
        if not value:
            return ""
        parts = value.split("@")
        if (len(value) > 254 or any(c.isspace() for c in value) or len(parts) != 2 or not parts[0]
                or "." not in parts[1] or parts[1].startswith(".") or parts[1].endswith(".")):
            return None
        return value

    @classmethod
    def validate(cls, pic, meta, share=True):
        for key, maximum in [("title", 80), ("category", 60), ("description", 500), ("author", 60)]:
            value = getattr(meta, key)
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{key.capitalize()} must be at most {maximum} characters")
        if not meta.title.strip():
            raise ValueError("Give it a name first")
        tags = meta.tags or []
        if (len(tags) > 20 or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in tags)
                or len({t.casefold() for t in tags}) != len(tags)):
            raise ValueError("Use at most 20 unique, nonempty tags, each at most 40 characters")
        if len(pic.png) > cls.max_png_bytes or (pic.svg is not None and len(pic.svg.encode()) > cls.max_svg_bytes):
            raise ValueError("Pictures must be at most 2 MiB; simplify the drawing first")
        with Image.open(BytesIO(pic.png)) as image:
            if image.format != "PNG" or max(image.size) > cls.max_image_side:
                raise ValueError("PNG must have sides no larger than 4096 pixels")
            image.verify()
        if max(pic.size) > cls.max_image_side or min(pic.size) <= 0:
            raise ValueError("Image sides must be between 1 and 4096 pixels")
        if share:
            if meta.license not in {x[0] for x in cls.licenses}:
                raise ValueError("Select CC0-1.0 or CC-BY-4.0")
            if cls.check_email(meta.email) is None:
                raise ValueError("The email address is invalid")
            if meta.license == "CC-BY-4.0":
                if not meta.author.strip() or meta.author == "anonymous":
                    raise ValueError("CC BY needs a name to credit, or choose CC0")
                if meta.generative_ai_used is not False:
                    raise ValueError(cls.cc_by_needs_no_ai)

    @staticmethod
    def slug(name):
        name = re.sub(r"[^\w]+", "-", name.lower(), flags=re.UNICODE).strip("-_") or "icon"
        return name + "-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()) + secrets.token_hex(3)

    @classmethod
    def save(cls, pic, row, catalog, id=None):
        cls.validate(pic, row, share=False)
        catalog = Path(catalog)
        if not catalog.is_file():
            raise ValueError("The BioCartoon catalog has not been downloaded; open BioCartoon Icons first")
        directory = catalog.parent.parent / "user_generated"
        slug = cls.slug(row.title)
        key = id or slug
        png = directory / (slug + ".png")
        svg = directory / (slug + ".svg") if pic.svg is not None else None
        created = []
        try:
            atomic_bytes(png, pic.png)
            created.append(png)
            if svg:
                atomic_bytes(svg, pic.svg.encode())
                created.append(svg)
            with sqlite3.connect(catalog, timeout=5) as db:
                db.execute("PRAGMA busy_timeout=5000")
                cols = {r[1] for r in db.execute("PRAGMA table_info(assets)")}
                credited = row.author and row.author != "anonymous"
                credit = '"' + row.title + '"' + (" by " + row.author if credited else "") + (", " + row.license if row.license else "")
                license_url = ("https://creativecommons.org/licenses/by/4.0/" if row.license == "CC-BY-4.0" else
                               "https://creativecommons.org/publicdomain/zero/1.0/" if row.license == "CC0-1.0" else None)
                record = {
                    "id": "user/" + key, "source": "user", "source_name": "User-generated", "source_id": slug,
                    "title": row.title, "variant": "", "description": row.description, "category": row.category,
                    "tags": json.dumps(row.tags or [], ensure_ascii=False), "license": row.license, "license_raw": row.license,
                    "license_url": license_url, "attribution_required": int(row.license == "CC-BY-4.0"), "share_alike": 0,
                    "author": row.author, "source_url": None, "doi": None, "credit": credit,
                    "svg_key": str(svg) if svg else None, "png_key": f"user/{key}.png", "png_is_raster": 0,
                    "png_width": pic.size[0], "png_height": pic.size[1], "svg_bytes": None, "png_bytes": None,
                    "svg_local": None, "png_local": str(png), "extra": '{"generated": true}', "uploaded_at": None,
                }
                if "generative_ai_used" in cols:
                    record["generative_ai_used"] = row.ai_used
                names = list(record)
                # Column names are a fixed program-owned set; every value is bound.
                db.execute("INSERT INTO assets (" + ",".join(names) + ") VALUES (" + ",".join("?" for _ in names) + ")", list(record.values()))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='assets_fts'").fetchone():
                    db.execute("INSERT INTO assets_fts(rowid,title,description,tags,category,variant) "
                               "SELECT rowid,title,description,tags,category,variant FROM assets WHERE id=?", (record["id"],))
            return BCIcon(id="user/" + key, title=row.title, source="user", source_name="User-generated", credit=credit,
                          license=row.license, category=row.category, author=row.author, png_local=str(png),
                          png_key=record["png_key"], svg_key=str(svg) if svg else None,
                          width=pic.size[0], height=pic.size[1], tags=row.tags or [])
        except Exception:
            for p in created:
                p.unlink(missing_ok=True)
            raise

    @staticmethod
    def _submission_db(state_file=None):
        p = Path(state_file) if state_file else biocartoon_data() / "manimble-submissions.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(p, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("CREATE TABLE IF NOT EXISTS submitted(fingerprint TEXT PRIMARY KEY, token TEXT NOT NULL, id TEXT, started REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS attempts(time REAL NOT NULL)")
        return db

    @classmethod
    def was_submitted(cls, path, state_file=None):
        fingerprint = cls.fingerprint(path)
        with cls._submission_db(state_file) as db:
            return db.execute("SELECT 1 FROM submitted WHERE fingerprint=?", (fingerprint,)).fetchone() is not None

    @classmethod
    def take_slot(cls, fingerprint, state_file=None):
        now = time.time()
        token = secrets.token_hex(16)
        with cls._submission_db(state_file) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM submitted WHERE fingerprint=?", (fingerprint,)).fetchone():
                raise ValueError(cls.already_submitted)
            db.execute("DELETE FROM attempts WHERE time<?", (now - 3600,))
            count, first = db.execute("SELECT count(*),min(time) FROM attempts").fetchone()
            if count >= cls.max_per_hour:
                raise ValueError(f"Too many submissions; try again in {math.ceil((3600 + first - now) / 60)} min")
            db.execute("INSERT INTO attempts VALUES(?)", (now,))
            db.execute("INSERT INTO submitted VALUES(?,?,NULL,?)", (fingerprint, token, now))
        return fingerprint, token

    @classmethod
    def finish_slot(cls, reservation, id=None, state_file=None):
        fingerprint, token = reservation
        with cls._submission_db(state_file) as db:
            if id is not None:
                db.execute("UPDATE submitted SET id=? WHERE fingerprint=? AND token=?", (id, fingerprint, token))
            else:
                db.execute("DELETE FROM submitted WHERE fingerprint=? AND token=? AND id IS NULL", (fingerprint, token))

    @staticmethod
    def _send(pic, meta, target, as_png=False):
        boundary = "biocartoon-" + secrets.token_hex(16)
        body = bytearray()
        def part(header, data):
            body.extend(f"--{boundary}\r\n{header}\r\n\r\n".encode())
            body.extend(data)
            body.extend(b"\r\n")
        value = asdict(meta)
        value["tags"] = value["tags"] or []
        part('Content-Disposition: form-data; name="kind"', b"asset")
        part('Content-Disposition: form-data; name="meta"', json.dumps(value, ensure_ascii=False).encode())
        svg = pic.svg is not None and not as_png
        name, mime, content = ("svg", "image/svg+xml", pic.svg.encode()) if svg else ("image", "image/png", pic.png)
        part(f'Content-Disposition: form-data; name="{name}"; filename="{name}"\r\nContent-Type: {mime}', content)
        body.extend(f"--{boundary}--\r\n".encode())
        req = Request(target.rstrip("/") + "/submit", data=bytes(body),
                      headers=HTTP_HEADERS | {"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
        try:
            with urlopen(req, timeout=120) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", f"HTTP {exc.code}")
            except (ValueError, AttributeError):
                detail = f"HTTP {exc.code}"
            raise ValueError(str(detail)) from exc
        try:
            answer = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError("The submission server sent invalid JSON; publication was not confirmed") from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or not answer["id"].strip():
            raise RuntimeError("The submission server sent an unexpected reply (missing JSON id)")
        return answer["id"]

    @classmethod
    def submit(cls, pic, meta, *, target=None, confirmed=False, fingerprint=None, as_png=False,
               reservation=None, state_file=None):
        """Blocking worker entry. Confirmation must name the target and complete metadata.

        SVG refusal never silently sends another asset: callers must explicitly
        confirm the PNG fallback and pass the same reservation.
        """
        if not confirmed:
            raise PermissionError("Confirm target, asset, metadata and license before publication")
        cls.validate(pic, meta)
        target = target or cls.submit_url()
        url = urlparse(target)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("The submission server must be an HTTP(S) URL without credentials")
        fingerprint = fingerprint or "sha256:" + hashlib.sha256((pic.svg.encode() if pic.svg else pic.png)).hexdigest()
        reservation = reservation or cls.take_slot(fingerprint, state_file)
        try:
            id = cls._send(pic, meta, target, as_png)
        except ValueError as exc:
            if pic.svg and not as_png:
                raise SVGRefused(str(exc), reservation) from exc
            cls.finish_slot(reservation, state_file=state_file)
            raise
        except Exception:
            cls.finish_slot(reservation, state_file=state_file)
            raise
        cls.finish_slot(reservation, id, state_file)
        return id

    @staticmethod
    def associate_submission(catalog, local_id, submitted_id):
        with sqlite3.connect(catalog, timeout=5) as db:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("UPDATE assets SET id=?,png_key=? WHERE id=?",
                       ("user/" + submitted_id, "user/" + submitted_id + ".png", local_id))


class BCLibraryDialog(QDialog):
    def __init__(self, file, picture, share, categories=(), within=None):
        super().__init__(within)
        self.file, self.pic, self.share = str(file), picture, share
        self.on_done = None
        self.setWindowTitle("Add to BioCartoon library" if share else "Add to your library")
        self.resize(860, 700)
        layout = QVBoxLayout(self)
        horizontal = QHBoxLayout()
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumSize(250, 250)
        image = QImage.fromData(picture.png)
        preview.setPixmap(QPixmap.fromImage(image).scaled(420, 540, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        horizontal.addWidget(preview, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        self.name_field = QLineEdit(Path(file).stem.replace("_", " ").replace("-", " "))
        self.name_field.setMaxLength(80)
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItem("")
        self.category.addItems([c.name for c in categories])
        self.category.lineEdit().setMaxLength(60)
        self.description_field = QPlainTextEdit()
        self.description_field.setMaximumHeight(100)
        self.tags_field = QLineEdit()
        self.tags_field.setPlaceholderText("Commas between; up to 20 tags")
        self.no_ai = QCheckBox("No generative AI was used to create this asset")
        self.license = QComboBox()
        for key, label in BCLibrary.licenses:
            self.license.addItem(label, key)
        self.credit_field = QLineEdit(prefs.get_str("biocartoonCredit"))
        self.credit_field.setMaxLength(60)
        self.email_field = QLineEdit(prefs.get_str("biocartoonEmail"))
        self.official = QCheckBox("Submit as official BioCartoon asset")
        for label, field in [("Name", self.name_field), ("Category", self.category), ("Description", self.description_field), ("Tags", self.tags_field)]:
            form.addRow(label, field)
        if share:
            form.addRow(self.no_ai)
            for label, field in [("License", self.license), ("Credit as", self.credit_field), ("Email (private)", self.email_field)]:
                form.addRow(label, field)
            form.addRow(self.official)
            disclosure = QLabel("Reviewed before publication. The asset, name, credit, description, categories, tags, AI disclosure and chosen license become public; email goes only to the BioCartoon team. You retain copyright. Reviewers may redirect official submissions to User-generated.")
            disclosure.setWordWrap(True)
            form.addRow(disclosure)
        else:
            form.addRow(QLabel("Saved on this computer only."))
        form.addRow(QLabel(f"Size: {picture.size[0]} × {picture.size[1]} px; {picture.bytes:,} bytes"))
        form.addRow(QLabel("SVG (vector)" if picture.svg else "PNG (raster)"))
        self.png_ok = QCheckBox("I confirm adding a PNG; SVG is recommended")
        self.reduced_ok = QCheckBox(f"I confirm the reduced size (from {picture.reduced_from})")
        if not picture.svg:
            form.addRow(self.png_ok)
        if picture.reduced_from:
            form.addRow(self.reduced_ok)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        form.addRow(self.problem)
        scroll.setWidget(form_widget)
        horizontal.addWidget(scroll, 1)
        layout.addLayout(horizontal)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.action = buttons.addButton("Submit" if share else "Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.action.clicked.connect(self.action_pressed)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for field in [self.name_field, self.tags_field, self.credit_field, self.email_field]:
            field.textChanged.connect(self.refresh)
        self.category.currentTextChanged.connect(self.refresh)
        self.description_field.textChanged.connect(self.refresh)
        for box in (self.no_ai, self.png_ok, self.reduced_ok, self.official):
            box.toggled.connect(self.refresh)
        self.license.currentIndexChanged.connect(self.refresh)
        self.refresh()

    @property
    def tags(self):
        seen, tags = set(), []
        for text in self.tags_field.text().split(","):
            text = text.strip()
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                tags.append(text)
        return tags

    def meta(self):
        return BCLibrary.Meta(title=self.name_field.text().strip(), category=self.category.currentText().strip(),
                              description=self.description_field.toPlainText().strip(), tags=self.tags,
                              author=self.credit_field.text().strip() or "anonymous", license=self.license.currentData(),
                              generative_ai_used=False if self.no_ai.isChecked() else None,
                              email=self.email_field.text().strip(), official=self.official.isChecked())

    def refresh(self, *_):
        why = None
        meta = self.meta()
        # Validation requiring image decoding stays in the worker; form validation is cheap.
        for key, n in [("title", 80), ("category", 60), ("description", 500), ("author", 60)]:
            if len(getattr(meta, key)) > n:
                why = f"{key.capitalize()} must be at most {n} characters"
        if not meta.title:
            why = "Give it a name first"
        if len(meta.tags) > 20 or any(len(t) > 40 for t in meta.tags):
            why = "Use at most 20 tags, each at most 40 characters"
        if self.share and meta.license == "CC-BY-4.0":
            if not self.no_ai.isChecked():
                why = BCLibrary.cc_by_needs_no_ai
            elif meta.author == "anonymous":
                why = "CC BY needs a name to credit, or choose CC0"
        if self.share and BCLibrary.check_email(meta.email) is None:
            why = "The email address is invalid"
        if self.pic.bytes > 2 * 1024 * 1024:
            why = "Too large: simplify the drawing first"
        if not self.pic.svg and not self.png_ok.isChecked():
            why = "Confirm adding it as a PNG first"
        if self.pic.reduced_from and not self.reduced_ok.isChecked():
            why = "Confirm adding it at the reduced size first"
        self.problem.setText(why or "")
        self.action.setEnabled(why is None)

    def action_pressed(self):
        self.refresh()
        if self.action.isEnabled() and self.on_done:
            self.on_done(self)


class BCLibraryMixin:
    def bio_cartoon_library_items(self, path):
        return [("Add to Local BioCartoon Library…", lambda: self.add_to_bio_cartoon_library(path, False)),
                ("Upload to Online BioCartoon Library…", lambda: self.add_to_bio_cartoon_library(path, True))]

    def add_to_bio_cartoon_library(self, path, share=False):
        spawn(self._open_bio_library(path, share))

    def _open_bio_library(self, path, share):
        try:
            cli = BioCartoonCLI(self.runtime)
            yield cli.usable()
            catalog = yield cli.catalog_path()
            if not catalog:
                raise ValueError("The BioCartoon catalog is not downloaded: open BioCartoon Icons first")
            if share and (yield run_blocking(BCLibrary.was_submitted, path)):
                raise ValueError(BCLibrary.already_submitted)
            pic = yield run_blocking(BCLibrary.read, path)
            categories = yield cli.categories()
            dialog = BCLibraryDialog(path, pic, share, categories, self.window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.on_done = lambda d: self.finish_bio_cartoon_library(d, path, catalog)
            dialog.open()
        except Exception as exc:
            self.toast(str(exc), error=True)

    def finish_bio_cartoon_library(self, d, path, catalog):
        meta = d.meta()
        target = BCLibrary.submit_url()
        if d.share:
            text = (f"Publish for review to {target}/submit?\n\nAsset: {path}\n"
                    + json.dumps(asdict(meta), ensure_ascii=False, indent=2)
                    + "\n\nYour asset and the metadata above (except email) will be public if approved. "
                    "You confirm you may share it under " + meta.license + ".")
            if QMessageBox.question(d, "Confirm BioCartoon publication", text,
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
                return False
        d.action.setEnabled(False)
        spawn(self._finish_bio_library(d, path, catalog, meta, target))
        return True

    def _finish_bio_library(self, dialog, path, catalog, meta, target):
        local = None
        reservation = None
        try:
            pic = dialog.pic
            yield run_blocking(BCLibrary.validate, pic, meta, dialog.share)
            row = BCLibrary.Row(meta.title, meta.category, meta.description,
                                meta.license if dialog.share else "", meta.author, meta.tags, meta.generative_ai_used)
            # Save first, including the vector; a failed network request never loses it.
            local = yield run_blocking(BCLibrary.save, pic, row, catalog)
            if dialog.share:
                fingerprint = yield run_blocking(BCLibrary.fingerprint, path)
                try:
                    id = yield run_blocking(BCLibrary.submit, pic, meta, target=target,
                                             confirmed=True, fingerprint=fingerprint)
                except SVGRefused as exc:
                    reservation = exc.reservation
                    answer = QMessageBox.question(dialog, "SVG refused — submit PNG instead?",
                        f"{target}/submit refused the SVG: {exc}\n\nSubmit its PNG preview instead, with the same metadata and {meta.license} license? Your local library keeps the SVG.",
                        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
                    if answer != QMessageBox.StandardButton.Ok:
                        yield run_blocking(BCLibrary.finish_slot, reservation)
                        reservation = None
                        self.toast("Kept in your local library; not submitted")
                        dialog.accept()
                        return
                    id = yield run_blocking(BCLibrary.submit, pic, meta, target=target, confirmed=True,
                                             fingerprint=fingerprint, as_png=True, reservation=reservation)
                    reservation = None
                prefs.set("biocartoonCredit", meta.author if meta.author != "anonymous" else "")
                prefs.set("biocartoonEmail", meta.email)
                submitted = prefs.get_json("biocartoonSubmitted", [])
                if fingerprint not in submitted:
                    prefs.set("biocartoonSubmitted", submitted + [fingerprint])
                # The submission itself succeeded even if updating its local association fails.
                try:
                    yield run_blocking(BCLibrary.associate_submission, catalog, local.id, id)
                except Exception as exc:
                    self.toast(f"Submitted for review ({id}); local record retained but could not associate its id: {exc}", error=True)
                else:
                    self.toast(f"Submitted for review ({id}); kept in your library")
            else:
                self.toast("Added “" + meta.title + "” to your library")
            dialog.accept()
        except Exception as exc:
            self.toast(("Saved in your local library, but not submitted: " if local else "Could not save: ") + str(exc), error=True)
            if local:
                dialog.accept()
            else:
                dialog.action.setEnabled(True)
        finally:
            if reservation:
                yield run_blocking(BCLibrary.finish_slot, reservation)
            pane = getattr(self, "biocartoon_pane", None)
            if pane:
                pane.reload()
