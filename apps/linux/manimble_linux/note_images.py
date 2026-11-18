"""Speaker-note image attachments with reversible Markdown serialization."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QImageReader, QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat
from PySide6.QtWidgets import QFileDialog, QTextEdit

from .clipboard import PICTURE_EXTENSIONS, SlidePaste, normalize_image, save_asset
from . import workers

_PATH_PROPERTY = int(QTextCharFormat.Property.UserProperty) + 101
_MARKUP_PROPERTY = _PATH_PROPERTY + 1


class NoteImageAttachment(QTextImageFormat):
    def __init__(self, path, image, max_width=600, markup=None):
        super().__init__()
        self.path = path
        self.image = image
        self.setName(path)
        scale = min(1, max_width / max(1, image.width()), 240 / max(1, image.height()))
        self.setWidth(round(image.width() * scale))
        self.setHeight(round(image.height() * scale))
        self.setProperty(_PATH_PROPERTY, path)
        self.setProperty(_MARKUP_PROPERTY, markup or f"![]({path})")


class NoteImages:
    regex = re.compile(r"!\[[^\]\n]*\]\(([^)\n]+)\)")
    types = PICTURE_EXTENSIONS

    @staticmethod
    def editor_width(editor):
        return min(360, max(80, editor.viewport().width() - 12))

    @staticmethod
    def has(text):
        return NoteImages.regex.search(text) is not None

    @staticmethod
    def attachment(path, base=None, max_width=600, markup=None):
        file = Path(path).expanduser()
        if not file.is_absolute():
            if base is None:
                return None
            file = Path(base) / file
        reader = QImageReader(str(file))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return None
        return NoteImageAttachment(path, image, max_width, markup)

    @staticmethod
    def attributed(text, base=None, attributes=None, max_width=600):
        document = QTextDocument()
        cursor = QTextCursor(document)
        fmt = attributes if isinstance(attributes, QTextCharFormat) else QTextCharFormat()
        if isinstance(attributes, dict):
            if attributes.get("font"):
                fmt.setFont(attributes["font"])
            if attributes.get("color"):
                fmt.setForeground(attributes["color"])
        cursor.setCharFormat(fmt)
        at = 0
        for match in NoteImages.regex.finditer(text):
            attachment = NoteImages.attachment(match[1], base, max_width, match[0])
            if attachment is None:
                continue
            cursor.insertText(text[at:match.start()], fmt)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(attachment.name()), attachment.image)
            cursor.insertImage(attachment)
            at = match.end()
        cursor.insertText(text[at:], fmt)
        document.setModified(False)
        return document

    @staticmethod
    def markup(document):
        if isinstance(document, QTextEdit):
            document = document.document()
        out = []
        block = document.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    path = fmt.property(_PATH_PROPERTY)
                    if fmt.isImageFormat() and path:
                        out.append(str(fmt.property(_MARKUP_PROPERTY) or f"![]({path})") * fragment.text().count("\ufffc"))
                    else:
                        out.append(fragment.text())
                it += 1
            block = block.next()
            if block.isValid():
                out.append("\n")
        return "".join(out)

    @staticmethod
    def save(data, ext, into):
        return save_asset(data, ext or "png", into, "note")

    @staticmethod
    def pictures(mime):
        """Snapshot eligible picture inputs, without retaining the live clipboard."""
        files = [("file", url.toLocalFile()) for url in mime.urls() if url.isLocalFile()
                 and Path(url.toLocalFile()).suffix.lower().lstrip(".") in NoteImages.types]
        if files or mime.hasText():
            return files
        data = SlidePaste.png_data(mime)
        return [("data", data)] if data else []

    @staticmethod
    def _save_picture(picture, into):
        kind, value = picture
        if kind == "data":
            return NoteImages.save(value, "png", into)
        file = Path(value)
        data = file.read_bytes()
        # Preserve the original bytes and extension when Qt can display them.
        # HEIF and other formats without a local Qt reader still work as PNG.
        if QImage.fromData(data).isNull():
            return NoteImages.save(normalize_image(data), "png", into)
        return NoteImages.save(data, file.suffix.lower().lstrip("."), into)

    @staticmethod
    def import_pictures(pictures, into):
        paths = []
        for picture in pictures:
            paths.append((yield workers.run_blocking(NoteImages._save_picture, picture, into)))
        return paths

    @staticmethod
    def import_images(from_, into):
        mime = from_.mimeData() if hasattr(from_, "mimeData") else from_
        pictures = NoteImages.pictures(mime)
        if pictures:
            return (yield from NoteImages.import_pictures(pictures, into))
        return None

    @staticmethod
    def choose(into, parent=None):
        files, _ = QFileDialog.getOpenFileNames(
            parent, "Choose pictures for the speaker notes", "",
            "Pictures (" + " ".join(f"*.{ext}" for ext in sorted(NoteImages.types)) + ")",
        )
        return (yield from NoteImages.import_pictures([("file", file) for file in files], into))

    @staticmethod
    def insert(paths, tv, base, max_width=600):
        if not paths:
            return
        cursor = tv.textCursor()
        document = tv.document()
        fmt = cursor.charFormat()
        if fmt.isImageFormat():
            fmt = QTextCharFormat()
        cursor.beginEditBlock()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        previous = document.characterAt(start - 1) if start > 0 else "\n"
        following = document.characterAt(end) if end < document.characterCount() - 1 else ""
        cursor.removeSelectedText()
        if previous not in ("\n", "\u2029"):
            cursor.insertText("\n", fmt)
        for index, path in enumerate(paths):
            if index:
                cursor.insertText("\n", fmt)
            attachment = NoteImages.attachment(path, base, max_width)
            if attachment:
                document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(path), attachment.image)
                cursor.insertImage(attachment)
            else:
                cursor.insertText(f"![]({path})", fmt)
        if following not in ("\n", "\u2029"):
            cursor.insertText("\n", fmt)
        cursor.setCharFormat(fmt)
        cursor.endEditBlock()
        tv.setTextCursor(cursor)
