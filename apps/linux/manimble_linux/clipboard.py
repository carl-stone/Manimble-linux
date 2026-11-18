"""Structured slide clipboard, rich text, and content-addressed picture assets."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QSize
from PySide6.QtGui import QFont, QImage, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit

from . import workers

OBJECT_MIME = "application/x-manimble-objects"
OBJECT_NAME = "com.manimble.objects"
PICTURE_EXTENSIONS = {"png", "jpg", "jpeg", "tiff", "tif", "gif", "heic", "heif", "bmp", "webp"}


def png(image):
    if image.isNull():
        raise ValueError("Clipboard picture could not be decoded")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("Could not encode clipboard picture")
    return bytes(buffer.data())


def normalize_image(data):
    """Normalize orientation and HEIF in the worker, not just in the GUI process."""
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info or "A" in image.getbands() else "RGB")
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()


def save_asset(data, ext, directory, prefix="pasted"):
    """Atomic, collision-safe content addressing; existing assets are never overwritten."""
    directory = Path(directory)
    ext = ext.lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]+", ext):
        raise ValueError("Invalid picture extension")
    digest = hashlib.sha256(data).hexdigest()
    name = f"{prefix}_{digest[:8]}.{ext}"
    destination = directory / name
    if destination.exists():
        if destination.read_bytes() == data:
            return name
        name = f"{prefix}_{digest}.{ext}"
        destination = directory / name
        if destination.exists():
            if destination.read_bytes() == data:
                return name
            raise OSError(f"Content-address collision at {destination}")
    fd, temporary = tempfile.mkstemp(prefix=".paste-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.link(temporary, destination)
    except FileExistsError:
        if destination.read_bytes() != data:
            raise OSError(f"Content-address collision at {destination}")
    finally:
        os.unlink(temporary)
    return name


@dataclass
class CopiedObjects:
    deck: str
    scene: str
    items: list[dict]
    type = OBJECT_NAME
    mime_type = OBJECT_MIME

    @classmethod
    def decode(cls, raw):
        data = json.loads(bytes(raw))
        if not isinstance(data, dict) or not isinstance(data.get("deck"), str) or not isinstance(data.get("scene"), str) or not isinstance(data.get("items"), list):
            raise ValueError("Invalid Manimble clipboard data")
        for item in data["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("pictures"), list):
                raise ValueError("Invalid Manimble clipboard item")
            for picture in item["pictures"]:
                if len(picture.get("box", [])) != 4:
                    raise ValueError("Invalid clipboard picture bounds")
                base64.b64decode(picture["png"], validate=True)
        return cls(data["deck"], data["scene"], data["items"])

    def encode(self):
        return json.dumps({"deck": self.deck, "scene": self.scene, "items": self.items}, ensure_ascii=False).encode()


class _ImageSources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            src = dict(attrs).get("src", "")
            if urllib.parse.urlsplit(src).scheme.lower() in ("http", "https", "data"):
                self.sources.append(src)


@dataclass
class SlidePaste:
    kind: str
    value: object
    styles: dict | None = None

    @staticmethod
    def image_sources(html):
        parser = _ImageSources()
        parser.feed(html)
        return parser.sources

    @staticmethod
    def styled_text(html):
        # Strip images before importing HTML: parsing must not perform network I/O.
        bare = re.sub(r"<img\b[^>]*>", "", html, flags=re.I)
        document = QTextDocument()
        document.setHtml(bare)
        full = document.toPlainText()
        lo, hi = len(full) - len(full.lstrip()), len(full.rstrip())
        if hi <= lo:
            return None
        text = full[lo:hi].replace("\u00a0", " ").replace("\u2028", "\n").replace("\u2029", "\n")
        styles, colors, weights = {"bold": [], "italic": []}, [], Counter()
        cursor = QTextCursor(document)
        offset = len(full[:lo].encode("utf-16-le")) // 2
        def add(runs, start, end, color=None):
            if runs and runs[-1]["end"] == start and runs[-1].get("color") == color:
                runs[-1]["end"] = end
            else:
                runs.append(dict(start=start, end=end, **({"color": color} if color else {})))
        for i, character in enumerate(full[lo:hi]):
            cursor.setPosition(offset)
            size = len(character.encode("utf-16-le")) // 2
            cursor.setPosition(offset + size, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat()
            if fmt.fontWeight() >= QFont.Weight.Bold:
                add(styles["bold"], i, i + 1)
            if fmt.fontItalic():
                add(styles["italic"], i, i + 1)
            color = fmt.foreground().color().name().upper() if fmt.hasProperty(fmt.Property.ForegroundBrush) else ""
            weights[color] += 1
            colors.append(color)
            offset += size
        main = weights.most_common(1)[0][0]
        colored = []
        for i, color in enumerate(colors):
            if color and color != main:
                add(colored, i, i + 1, color)
        if colored:
            styles["color"] = colored
        return text, {key: runs for key, runs in styles.items() if runs}

    @staticmethod
    def png_data(mime):
        if mime.hasFormat("image/png"):
            return bytes(mime.data("image/png"))
        if mime.hasImage():
            return png(QImage(mime.imageData()))
        if mime.hasFormat("image/tiff"):
            return png(QImage.fromData(mime.data("image/tiff")))
        return None

    png = staticmethod(png)
    save = staticmethod(save_asset)

    @classmethod
    def read(cls, mime=None):
        if mime is None:
            mime = QApplication.clipboard().mimeData()
        for format in (OBJECT_MIME, OBJECT_NAME):
            if mime.hasFormat(format):
                try:
                    return cls("objects", CopiedObjects.decode(mime.data(format)))
                except (ValueError, TypeError, KeyError):
                    pass  # A damaged private flavour may still have valid public text/image data.
        files = [("file", url.toLocalFile()) for url in mime.urls()
                 if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower().lstrip(".") in PICTURE_EXTENSIONS]
        if files:
            return cls("pictures", files)
        text = mime.text().strip()
        styled = cls.styled_text(mime.html()) if mime.hasHtml() else None
        if text or styled:
            return cls("text", styled[0], styled[1]) if styled else cls("text", text.replace("\u00a0", " ").replace("\u2028", "\n").replace("\u2029", "\n"), {})
        sources = cls.image_sources(mime.html()) if mime.hasHtml() else []
        if sources:
            return cls("pictures", [("remote", src) for src in sources])
        data = cls.png_data(mime)
        if data:
            return cls("pictures", [("data", data)])
        if mime.hasFormat("application/pdf"):
            return cls("pictures", [("pdf", bytes(mime.data("application/pdf")))])
        return None


def pasted_picture(picture, directory, prefix="pasted"):
    kind, value = picture
    if kind == "file":
        data = Path(value).read_bytes()
    elif kind == "remote":
        scheme = urllib.parse.urlsplit(value).scheme.lower()
        if scheme not in ("http", "https", "data"):
            raise ValueError("Only HTTP(S) and data images can be pasted")
        with urllib.request.urlopen(value, timeout=60) as response:
            if urllib.parse.urlsplit(response.url).scheme.lower() not in ("http", "https", "data"):
                raise ValueError("Unsupported image redirect")
            data = response.read()
    elif kind == "pdf":
        from PySide6.QtPdf import QPdfDocument
        document = QPdfDocument()
        buffer = QBuffer()
        buffer.setData(value)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        document.load(buffer)
        if document.pageCount() < 1:
            raise ValueError(f"Clipboard PDF could not be read: {document.error()}")
        size = document.pagePointSize(0)
        data = png(document.render(0, QSize(max(1, round(size.width() * 2)), max(1, round(size.height() * 2)))))
    else:
        data = bytes(value)
    return save_asset(normalize_image(data), "png", directory, prefix)


def _embed_items(items):
    out = []
    for item in items:
        copy = dict(item)
        copy["pictures"] = [dict(p, png=base64.b64encode(Path(p["png"]).read_bytes()).decode("ascii")) for p in item.get("pictures", [])]
        out.append(copy)
    return out


class ClipboardMixin:
    def _clipboard_text_target(self):
        widget = QApplication.focusWidget()
        return widget if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)) else None

    def copy(self, *_):
        target = self._clipboard_text_target()
        if target:
            target.copy()
            return
        return self.copy_selection()

    def cut(self, *_):
        target = self._clipboard_text_target()
        if target:
            target.cut()
            return
        ws, context = self.workspace, self.edit_context
        oids = sorted(self.canvas.selected_ids, reverse=True)
        def delete_copied():
            def work():
                if self.workspace is not ws:
                    return
                mark = yield self.call("undo_mark")
                try:
                    for oid in oids:
                        request = dict(op="delete", oid=oid, scene=context.scene, stop=context.stop)
                        if context.seen:
                            request["seen"] = context.seen
                        yield from self.run_edit(request)
                finally:
                    yield self.call("undo_merge", dict(mark=mark["mark"], label="cut"))
            self.submit(work)
        return self.copy_selection(then=delete_copied)

    def copy_selection(self, to=None, then=None):
        if not self.workspace or not self.scene_name or not self.canvas.selected_ids:
            return
        ws, context = self.workspace, self.edit_context
        oids = list(self.canvas.selected_ids)
        def work():
            request = dict(scene=context.scene, stop=context.stop, oids=oids)
            if context.seen:
                request["seen"] = context.seen
            reply = yield self.call("copy", request)
            if not reply.get("ok") or not reply.get("items"):
                self.toast(reply.get("error", "These elements are not on this build"), error=True)
                return
            items = yield workers.run_blocking(_embed_items, reply["items"])
            copied = CopiedObjects(str(ws.original), context.scene, items)
            mime = QMimeData()
            for format in (OBJECT_MIME, OBJECT_NAME):
                mime.setData(format, copied.encode())
            texts = [item["text"] for item in items if item.get("text")]
            if texts:
                mime.setText("\n".join(texts))
            if items[0].get("pictures"):
                data = base64.b64decode(items[0]["pictures"][0]["png"])
                mime.setData("image/png", data)
                mime.setImageData(QImage.fromData(data))
            (to or QApplication.clipboard()).setMimeData(mime)
            why = next((item.get("why") for item in items if not item.get("code") and item.get("why")), None)
            if why:
                self.toast(f"Copied; part pastes as a picture: {why}")
            if then and self.workspace is ws:
                then()
            return copied
        return self.submit(work)

    def paste(self, *_):
        target = self._clipboard_text_target()
        if target:
            target.paste()
            return
        return self.paste_on_slide()

    @property
    def can_paste_on_slide(self):
        if not self.scene_name or self.player:
            return False
        mime = QApplication.clipboard().mimeData()
        return any(mime.hasFormat(f) for f in (OBJECT_MIME, OBJECT_NAME, "text/uri-list", "text/plain", "text/html", "image/png", "image/tiff", "application/pdf")) or mime.hasImage()

    def _paste_offset(self, objects=False):
        pastes = getattr(self, "_pastes", None)
        if pastes is None:
            self._pastes = pastes = {}
        key = self.scene_name
        k = pastes.get(key, 0)
        pastes[key] = (k + 1) % 6
        return ((k + 1) if objects else k) * 0.04 * self.canvas.frame_size[0]

    def _paste_context_request(self, context, items):
        request = dict(scene=context.scene, stop=context.stop, items=items)
        if context.seen:
            request["seen"] = context.seen
        return request

    def _insert_pasted(self, context, items):
        reply = yield self.call("insert_many", self._paste_context_request(context, items))
        if reply.get("ok"):
            yield from self.reload_state()
            if self.scene_name == context.scene:
                yield from self.load_scene(context.scene, True)
        return reply

    def paste_objects(self, copied, into=None):
        ws = into or self.workspace
        context = self.edit_context
        delta = self._paste_offset(True) if copied.scene == context.scene else 0
        same_deck = Path(copied.deck).resolve() == Path(ws.original).resolve()
        unit = self.canvas.unit_px
        def work():
            fallback, items = [], []
            for item in copied.items:
                pictures = []
                for picture in item.get("pictures", []):
                    file = yield workers.run_blocking(save_asset, base64.b64decode(picture["png"], validate=True), "png", ws.work)
                    b = [float(x) + delta for x in picture["box"]]
                    pictures.append(dict(kind="image", file=file, sx=b[0], sy=(b[1]+b[3])/2, ex=b[2], ey=(b[1]+b[3])/2))
                fallback.extend(pictures)
                if same_deck and item.get("code") and len(item.get("at", [])) == 2:
                    x, y = item["at"]
                    items.append(dict(kind="code", code=f"{item['code']}.move_to(({x + delta/unit:.3f}, {y - delta/unit:.3f}, 0))", box=[float(v)+delta for v in item["box"]]))
                else:
                    items.extend(pictures)
            if self.workspace is not ws:
                raise RuntimeError("The deck changed while preparing the paste")
            if not items:
                raise ValueError("The copied elements contain no pasteable code or pictures")
            reply = yield from self._insert_pasted(context, items)
            if not reply.get("ok") and any(it["kind"] == "code" for it in items) and fallback:
                # insert_many rolls back a failed render, so the image retry remains one undo unit.
                self.toast(f"Pasting pictures instead: {reply.get('error', 'copied code did not run')}")
                reply = yield from self._insert_pasted(context, fallback)
            if not reply.get("ok"):
                self.toast(reply.get("error", "Paste failed"), error=True)
            return reply
        return self.submit(work)

    def paste_on_slide(self, from_=None):
        if not self.workspace or not self.scene_name:
            return
        mime = from_.mimeData() if from_ is not None and hasattr(from_, "mimeData") else from_
        what = SlidePaste.read(mime)
        if what is None:
            self.toast("Nothing on the clipboard can go on a slide: copy a picture or text", error=True)
            return
        if what.kind == "objects":
            return self.paste_objects(what.value)
        ws, context = self.workspace, self.edit_context
        width, height = self.canvas.frame_size
        items = []
        if what.kind == "text":
            d = self._paste_offset()
            x, y = width/2+d, height/2+d
            items.append(dict(kind="text", value=what.value, styles=what.styles or {}, sx=x-1, sy=y, ex=x+1, ey=y))
        else:
            for _ in what.value:
                d = self._paste_offset()
                x, y = width/2+d, height/2+d
                half = 2*self.canvas.unit_px
                items.append(dict(kind="image", sx=x-half, sy=y, ex=x+half, ey=y))
        def work():
            if what.kind == "pictures":
                for item, picture in zip(items, what.value):
                    item["file"] = yield workers.run_blocking(pasted_picture, picture, ws.work)
            if self.workspace is not ws:
                raise RuntimeError("The deck changed while preparing the paste")
            reply = yield from self._insert_pasted(context, items)
            if not reply.get("ok"):
                self.toast(reply.get("error", "Paste failed"), error=True)
            return reply
        return self.submit(work)

    def pasted_picture(self, picture, into):
        return workers.run_blocking(pasted_picture, picture, into)
