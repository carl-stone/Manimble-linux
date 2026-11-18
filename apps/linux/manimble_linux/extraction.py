"""Manual/automatic picture extraction, local mask history, and one engine undo unit."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import tempfile
import shutil

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDialog, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from .extract_runtime import DISCLOSURE, ExtractRuntime
from .icons import template_icon
from .models import Box, stop_box
from .style import Theme, is_dark, watch_appearance
from .tasks import spawn
from .widgets import hairline
from .workers import WorkerProcess, run_blocking


@dataclass
class Part:
    image: Image.Image
    box: tuple[int,int,int,int]  # x0,y0,x1,y1, top-left picture pixels


class ImageExtractor:
    Part = Part

    @staticmethod
    def load(path):
        with Image.open(path) as image:
            return ImageOps.exif_transpose(image).convert("RGBA")

    @staticmethod
    def rectangle(w,h,box):
        x,y,bw,bh = box
        x0,y0 = max(0,math.floor(x)),max(0,math.floor(y))
        x1,y1 = min(w,math.ceil(x+bw)),min(h,math.ceil(y+bh))
        mask = np.zeros((h,w),np.uint8)
        if x1 > x0 and y1 > y0:
            mask[y0:y1,x0:x1] = 255
        return mask

    @staticmethod
    def bounds(mask):
        ys,xs = np.nonzero(np.asarray(mask)>127)
        if not len(xs):
            return None
        h,w = mask.shape
        return max(0,int(xs.min())-2),max(0,int(ys.min())-2),min(w,int(xs.max())+3),min(h,int(ys.max())+3)

    @staticmethod
    def cut_out(image,mask):
        rgba = np.asarray(image.convert("RGBA"),dtype=np.uint8)
        mask = np.asarray(mask,dtype=np.uint8)
        if mask.shape != rgba.shape[:2]:
            raise ValueError("Mask dimensions differ from the picture")
        box = ImageExtractor.bounds(mask)
        if box is None:
            return None
        x0,y0,x1,y1 = box
        crop = rgba[y0:y1,x0:x1].copy()
        crop[:,:,3] = (crop[:,:,3].astype(np.uint16)*mask[y0:y1,x0:x1].astype(np.uint16)//255).astype(np.uint8)
        return Part(Image.fromarray(crop),box)

    @staticmethod
    def hole(masks,w,h):
        union = np.zeros((h,w),np.bool_)
        for mask in masks:
            if np.shape(mask) != (h,w):
                raise ValueError("Mask dimensions differ from the picture")
            union |= np.asarray(mask)>40
        radius = max(2,min(w,h)//160)
        # Pillow's separable max filter implements the same square dilation.
        return np.asarray(Image.fromarray(union.astype(np.uint8)*255).filter(ImageFilter.MaxFilter(2*radius+1)))

    @staticmethod
    def overlap(a,b):
        a,b = np.asarray(a)>127,np.asarray(b)>127
        return int(np.count_nonzero(a&b))/max(1,min(int(a.sum()),int(b.sum())))


class Tool(Enum):
    box = "Box"
    keep = "Keep"
    remove = "Remove"


@dataclass
class Element:
    mask: np.ndarray
    region: tuple | None = None
    color: int = 0
    _bounds: tuple | None = None

    def __post_init__(self):
        self.refresh()

    def refresh(self):
        self._bounds = ImageExtractor.bounds(self.mask)

    @property
    def bounds(self):
        return self._bounds

    def copy(self):
        return Element(self.mask.copy(),self.region,self.color)


class ExtractionState:
    """Widget-independent mask editing; history contains at most forty gestures."""
    def __init__(self,image,masks=()):
        self.image = image.convert("RGBA")
        self.w,self.h = image.size
        self.elements = [Element(np.asarray(m,np.uint8).copy(),None,i) for i,m in enumerate(masks)]
        if any(e.mask.shape != (self.h,self.w) for e in self.elements):
            raise ValueError("Mask dimensions differ from the picture")
        self.selected = 0 if self.elements else None
        self.tool = Tool.box
        self.brush = 14
        self.undo_stack,self.redo_stack = [],[]
        self.generation,self.filled_for,self.filled = 0,-1,None
        self.on_change = None
        self.color_index = len(self.elements)

    def _snapshot(self):
        return [e.copy() for e in self.elements],self.selected

    def checkpoint(self):
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-40]
        self.redo_stack.clear()

    def changed(self):
        self.generation += 1
        if self.on_change:
            self.on_change()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self.elements,self.selected = self.undo_stack.pop()
            self.changed()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self.elements,self.selected = self.redo_stack.pop()
            self.changed()

    def make_element(self,mask,region=None):
        mask = np.asarray(mask,dtype=np.uint8)
        if mask.shape != (self.h,self.w):
            raise ValueError("Mask dimensions differ from the picture")
        e = Element(mask.copy(),region,self.color_index)
        self.color_index += 1
        return e

    def add_region(self,box,whole_box=True):
        if not whole_box:
            raise ValueError("Automatic box prediction must run in the model worker")
        self.add_masks([ImageExtractor.rectangle(self.w,self.h,box)],region=box)

    def add_masks(self,masks,region=None,deduplicate=False):
        fresh = []
        for mask in masks:
            if ImageExtractor.bounds(mask) is None:
                continue
            if deduplicate and any(ImageExtractor.overlap(mask,e.mask)>0.5 for e in self.elements+fresh):
                continue
            fresh.append(self.make_element(mask,region))
        if fresh:
            self.checkpoint()
            self.elements.extend(fresh)
            self.selected = len(self.elements)-1
            self.changed()
        return len(fresh)

    def element(self,p):
        x,y = map(int,p)
        if 0 <= x < self.w and 0 <= y < self.h:
            for i in range(len(self.elements)-1,-1,-1):
                if self.elements[i].mask[y,x]>127:
                    return i
        boxes = [(i,e.bounds) for i,e in enumerate(self.elements)]
        hits = [(i,b) for i,b in boxes if b and b[0]<=p[0]<=b[2] and b[1]<=p[1]<=b[3]]
        return min(hits,key=lambda v:(v[1][2]-v[1][0])*(v[1][3]-v[1][1]))[0] if hits else None

    def begin_stroke(self):
        self.checkpoint()
        if self.tool == Tool.keep and self.selected is None:
            self.elements.append(self.make_element(np.zeros((self.h,self.w),np.uint8)))
            self.selected = len(self.elements)-1
        # Invalidate an existing preview immediately, before the first dab.
        self.generation += 1

    def paint(self,a,b,radius):
        targets = [self.selected] if self.selected is not None else list(range(len(self.elements))) if self.tool == Tool.remove else []
        rr = max(1,radius)
        x0,y0 = max(0,int(min(a[0],b[0])-rr)),max(0,int(min(a[1],b[1])-rr))
        x1,y1 = min(self.w-1,int(max(a[0],b[0])+rr)),min(self.h-1,int(max(a[1],b[1])+rr))
        if x1 < x0 or y1 < y0 or not targets:
            return
        ys,xs = np.mgrid[y0:y1+1,x0:x1+1]
        dx,dy = b[0]-a[0],b[1]-a[1]
        t = np.clip(((xs+0.5-a[0])*dx+(ys+0.5-a[1])*dy)/max(dx*dx+dy*dy,1e-6),0,1)
        hit = (xs+0.5-a[0]-t*dx)**2+(ys+0.5-a[1]-t*dy)**2 <= rr*rr
        for i in targets:
            self.elements[i].mask[y0:y1+1,x0:x1+1][hit] = 255 if self.tool == Tool.keep else 0

    def end_stroke(self):
        for element in self.elements:
            element.refresh()
        if any(e.bounds is None for e in self.elements):
            self.elements = [e for e in self.elements if e.bounds is not None]
            self.selected = None
        self.changed()

    def delete_selected(self):
        if self.selected is not None:
            self.checkpoint()
            del self.elements[self.selected]
            self.selected = min(self.selected,len(self.elements)-1) if self.elements else None
            self.changed()

    def whole_box_pressed(self):
        if self.selected is not None:
            e = self.elements[self.selected]
            b = e.bounds
            box = e.region or ((b[0],b[1],b[2]-b[0],b[3]-b[1]) if b else None)
            if box:
                self.checkpoint()
                e.mask = ImageExtractor.rectangle(self.w,self.h,box)
                e.refresh()
                self.changed()

    def clear_pressed(self):
        if self.elements:
            self.checkpoint()
            self.elements,self.selected = [],None
            self.changed()

    def accept_preview(self,generation,image):
        if generation != self.generation:
            return False
        self.filled,self.filled_for = image,generation
        return True


def build_insert_request(scene,stop,first_shown,oid,box,image_size,parts,background=None,seen=None):
    """Map crop pixels proportionally, independently on each axis, to frame geometry."""
    w,h = image_size
    if w <= 0 or h <= 0:
        raise ValueError("Picture dimensions must be positive")
    if not isinstance(box,Box):
        box = Box(*box)
    items = []
    for file,crop in parts:
        x0,y0,x1,y1 = crop
        items.append({"kind":"image","file":str(file),
            "sx":box.x0+x0*box.w/w,"sy":box.y0+y0*box.h/h,
            "ex":box.x0+x1*box.w/w,"ey":box.y0+y1*box.h/h})
    if not items:
        raise ValueError("Nothing to extract")
    request = {"scene":scene,"stop":min(first_shown,stop) if background else stop,"items":items}
    if background:
        request["replace_image"] = {"oid":oid,"file":str(background)}
    if seen:
        request["seen"] = seen
    return request


class _NothingToExtract(ValueError):
    pass


class _SaveExtractionError(OSError):
    def __init__(self, filename, error):
        super().__init__(str(error))
        self.filename = filename


def save_parts(image,masks,deck,stem,filled=None):
    """Write collision-safe editable assets; remove only our files on partial failure."""
    deck = Path(deck)
    created = []
    stem = Path(stem).stem or "picture"
    def save(img,suffix):
        k = 1
        while True:
            file = f"{stem}_{suffix}{k}.png"
            try:
                stream = (deck/file).open("xb")
                break
            except FileExistsError:
                k += 1
            except OSError as error:
                raise _SaveExtractionError(file, error) from error
        created.append(deck/file)
        try:
            with stream:
                img.save(stream, format="PNG")
        except Exception as error:
            raise _SaveExtractionError(file, error) from error
        return file
    try:
        parts = []
        for mask in masks:
            part = ImageExtractor.cut_out(image,mask)
            if part:
                parts.append((save(part.image,"part"),part.box))
        if not parts:
            raise _NothingToExtract("Nothing to extract")
        background = save(filled,"background") if filled is not None else None
        return parts,background
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _qimage(image):
    rgba = np.asarray(image.convert("RGBA"),np.uint8)
    return QImage(rgba.data,rgba.shape[1],rgba.shape[0],rgba.strides[0],QImage.Format.Format_RGBA8888).copy()


def _stage(image,masks,directory):
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    image_file = directory/"image.png"
    image.save(image_file)
    hole_file = directory/"hole.png"
    Image.fromarray(ImageExtractor.hole(masks,*image.size)).save(hole_file)
    return image_file,hole_file


class _Spinner(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._advance)

    def _advance(self):
        self._angle = (self._angle - 45) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(Theme.tertiary_ink(), 1.5))
        ring = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        p.drawEllipse(ring)
        pen = QPen(Theme.secondary_ink(), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(ring, self._angle * 16, 100 * 16)


@dataclass
class _ExtractionJob:
    text: str
    generation: int
    worker: WorkerProcess | None = None
    progress: float | None = None


class ExtractEditor(QDialog):
    Tool = Tool
    colors = ("#7D5CFF","#33C759","#FF992E","#4DB3FF","#FF458C","#F2D91A")

    @dataclass
    class Result:
        masks: list
        fill: bool
        filled: Image.Image | None

    def __init__(self, image, masks=(), source="the picture", runtime=None, extract_runtime=None, parent=None):
        super().__init__(parent)
        self.setFont(Theme.font(13))
        self.setWindowTitle("Extract Elements")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(960,680)
        self.setMinimumSize(720,520)
        self.state = ExtractionState(image,masks)
        self.runtime,self.extract_runtime = runtime,extract_runtime
        self.state.on_change = self.changed
        self._temporary = tempfile.TemporaryDirectory(prefix="manimble-extract-")
        self._workers = set()
        self._jobs = {}
        self._status_text = ""
        self._closed = False
        self._operations = 0
        self._insert_pending = False
        self._serial = 0
        self._finish = None
        self._result = None
        self._overlays = {}
        self._image = _qimage(self.state.image)
        self._filled_image = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        heading = QLabel(f"Extract elements from {source or 'the picture'}")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        heading.setFont(Theme.font(13, QFont.Weight.DemiBold))
        layout.addWidget(heading)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.segments = QWidget()
        segments = QHBoxLayout(self.segments)
        segments.setContentsMargins(0, 0, 0, 0)
        segments.setSpacing(0)
        self.tool_control = QButtonGroup(self)
        self._tool_buttons = {}
        tools = (
            (Tool.box, "rectangle.dashed", "Drag a box around something to add it as an element (B)"),
            (Tool.keep, "plus.circle", "Paint over parts the selected element should include (K)"),
            (Tool.remove, "minus.circle", "Paint over parts the selected element should leave out (R)"),
        )
        for index, (tool, symbol, tip) in enumerate(tools):
            button = self._button(tool.value, lambda t=tool: self.set_tool(t), size=12, tip=tip)
            button.setObjectName(("firstSegment", "middleSegment", "lastSegment")[index])
            button.setAccessibleName(tool.value)
            button.setIcon(template_icon(symbol, button))
            button.setIconSize(QSize(14, 14))
            button.setCheckable(True)
            button.setChecked(tool == Tool.box)
            self.tool_control.addButton(button)
            self._tool_buttons[tool] = button
            segments.addWidget(button)
        row.addWidget(self.segments)
        self.brush_label = QLabel("Brush")
        self.brush_label.setFont(Theme.font(13))
        row.addWidget(self.brush_label)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(3,60)
        self.brush_slider.setValue(14)
        self.brush_slider.setFixedWidth(110)
        self.brush_slider.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.brush_slider.setAccessibleName("Brush")
        self.brush_slider.valueChanged.connect(self.brush_changed)
        row.addWidget(self.brush_slider)
        row.addStretch()
        self.spinner = _Spinner(self)
        row.addWidget(self.spinner)
        self.detect_button = self._button("Find Elements", self.detect_pressed,
            tip="Add every separate subject the picture's model finds")
        row.addWidget(self.detect_button)
        self.whole_box = self._button("Whole Box", self.state.whole_box_pressed,
            tip="Cut out the selected element's whole box instead of the shape found inside it")
        row.addWidget(self.whole_box)
        self.delete_button = self._button("Delete", self.state.delete_selected)
        row.addWidget(self.delete_button)
        self.clear_button = self._button("Clear All", self.state.clear_pressed)
        row.addWidget(self.clear_button)
        layout.addLayout(row)
        self.canvas = ExtractCanvas(self)
        layout.addWidget(self.canvas,1)
        self.hint = QLabel()
        self.hint.setFont(Theme.font(11.5))
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.model_row = QWidget()
        model_row = QHBoxLayout(self.model_row)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        self.model_help = QLabel()
        self.model_help.setFont(Theme.font(11))
        self.model_help.setWordWrap(True)
        model_row.addWidget(self.model_help, 1)
        self.install_button = self._button("Download extraction model…", self.install_model)
        model_row.addWidget(self.install_button)
        layout.addWidget(self.model_row)

        self.work_row = QWidget()
        work_row = QHBoxLayout(self.work_row)
        work_row.setContentsMargins(0, 0, 0, 0)
        work_row.setSpacing(8)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setFont(Theme.font(11))
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        work_row.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setFont(Theme.font(10))
        self.progress.setFixedWidth(104)
        work_row.addWidget(self.progress)
        self.cancel_work = self._button("Cancel operation", self.cancel_operation)
        work_row.addWidget(self.cancel_work)
        layout.addWidget(self.work_row)
        layout.addWidget(hairline(self))

        row = QHBoxLayout()
        row.setSpacing(8)
        options = QHBoxLayout()
        options.setSpacing(16)
        self.fill_box = QCheckBox("Fill in the background behind them")
        self.fill_box.setToolTip("Replace the picture with a copy where a best guess of what is behind each element is painted in, so the elements can be moved away")
        self.fill_box.setChecked(True)
        self.fill_box.toggled.connect(self.fill_changed)
        options.addWidget(self.fill_box)
        self.preview_box = QCheckBox("Preview background")
        self.preview_box.toggled.connect(self.preview_changed)
        options.addWidget(self.preview_box)
        row.addLayout(options)
        row.addStretch()
        row.addWidget(self._button("Cancel", self.reject, size=13))
        self.insert_button = self._button("Insert", self.insert_pressed, size=13)
        self.insert_button.setDefault(True)
        row.addWidget(self.insert_button)
        layout.addLayout(row)
        self._shortcuts = []
        for key,fn in (("Ctrl+Z",self.state.undo),("Ctrl+Shift+Z",self.state.redo),("Ctrl+Y",self.state.redo)):
            shortcut = QShortcut(QKeySequence(key),self)
            shortcut.activated.connect(fn)
            self._shortcuts.append(shortcut)
        self.finished.connect(self._finished)
        self.refresh_controls()
        self._appearance_changed()
        watch_appearance(self._appearance_changed)

    def _button(self, text, action, *, size=11, tip=""):
        button = QPushButton(text)
        button.setFont(Theme.font(size))
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setAutoDefault(False)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: action())
        return button

    def _appearance_changed(self):
        border = Theme.separator().name(QColor.NameFormat.HexArgb)
        selected = Theme.accent_soft.name(QColor.NameFormat.HexArgb)
        pressed = Theme.ink(.09).name(QColor.NameFormat.HexArgb)
        sheet = f"""
            QPushButton {{
                background: transparent;
                border: 1px solid {border};
                min-height: 18px;
                padding: 2px 5px;
            }}
            QPushButton:checked {{ background: {selected}; }}
            QPushButton:pressed {{ background: {pressed}; }}
            QPushButton:focus {{ border-color: palette(highlight); }}
            QPushButton#firstSegment {{ border-top-left-radius: 5px; border-bottom-left-radius: 5px; }}
            QPushButton#middleSegment, QPushButton#lastSegment {{ border-left: none; }}
            QPushButton#lastSegment {{ border-top-right-radius: 5px; border-bottom-right-radius: 5px; }}
        """
        if self.segments.styleSheet() != sheet:
            self.segments.setStyleSheet(sheet)
        for label in (self.hint, self.model_help, self.status):
            self._ink_label(label, Theme.secondary_ink())
        self._refresh_brush()
        self.canvas.update()

    @staticmethod
    def _ink_label(label, color):
        palette = label.palette()
        if palette.color(QPalette.ColorRole.WindowText) != color:
            palette.setColor(QPalette.ColorRole.WindowText, color)
            label.setPalette(palette)

    def _refresh_brush(self):
        painting = self.state.tool != Tool.box
        self.brush_slider.setEnabled(painting)
        self._ink_label(self.brush_label, Theme.secondary_ink() if painting else Theme.tertiary_ink())

    def run(self, on=None, done=None):
        if on is not None and on is not self.parentWidget():
            self.setParent(on, self.windowFlags())
        if parent := self.parentWidget():
            room = parent.contentsRect()
            width, height = max(1, room.width()-20), max(1, room.height()-20)
            w, h = self.state.w, self.state.h
            fit = max(0, min((width-20-36)/w, (height-20-190)/h, 1.5))
            self.canvas.setMinimumHeight(min(300, max(0, height-220)))
            self.setMinimumSize(min(720, width), min(520, height))
            self.resize(min(width, max(720, round(w*fit+36))), min(height, max(520, round(h*fit+190))))
        self._finish = done
        self.show()
        self.canvas.setFocus()

    def _finished(self,code):
        self._closed = True
        self.cancel_operation()
        result = self._result if code == QDialog.DialogCode.Accepted else None
        done,self._finish = self._finish,None
        if done:
            done(result)
        # Processes must have exited before deleting their private image/mask paths.
        self._cleanup_when_stopped()

    def _cleanup_when_stopped(self):
        from PySide6.QtCore import QProcess
        if self._operations or any(w.proc is not None and w.proc.state()!=QProcess.ProcessState.NotRunning for w in self._workers):
            QTimer.singleShot(100,self._cleanup_when_stopped)
        else:
            self._temporary.cleanup()

    def cancel_operation(self):
        self._insert_pending = False
        self._jobs.clear()
        for worker in tuple(self._workers):
            worker.cancel()
        if not self._closed:
            self._status_text = "Operation cancelled; manual tools remain available."
            self.refresh_controls()

    def _begin_job(self, kind, text):
        job = _ExtractionJob(text, self.state.generation)
        self._jobs[kind] = job
        self._operations += 1
        self._status_text = ""
        self.refresh_controls()
        return job

    def _finish_job(self, kind, job):
        self._operations -= 1
        if self._jobs.get(kind) is job:
            del self._jobs[kind]
            self.refresh_controls()

    def _track(self, kind, job, worker):
        job.worker = worker
        self._workers.add(worker)

        def progress(fraction, text):
            if self._jobs.get(kind) is not job:
                return
            job.progress = fraction
            if text:
                job.text = text
            self._refresh_work()

        worker.progress.connect(progress)
        future = worker.start()
        worker.proc.finished.connect(lambda *_: self._workers.discard(worker))
        return future

    def _refresh_work(self):
        job = self._jobs.get("model") or self._jobs.get("preview")
        text = job.text if job else self._status_text
        self.status.setText(text)
        self.work_row.setVisible(bool(text))
        self.spinner.setVisible(job is not None)
        self.cancel_work.setVisible(job is not None)
        self.progress.setVisible(job is not None)
        if job:
            if job.progress is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(round(job.progress * 100))

    def set_tool(self,tool):
        self.state.tool = tool
        self._tool_buttons[tool].setChecked(True)
        self.refresh_controls()
        self.canvas.refresh_cursor()
        self.canvas.update()

    def brush_changed(self,value):
        self.state.brush = value
        self.canvas.update()

    def nudge_brush(self,delta):
        self.brush_slider.setValue(self.brush_slider.value() + delta)

    def changed(self):
        self._insert_pending = False
        self._overlays.clear()
        self.canvas.update()
        self.refresh_controls()
        if self.preview_box.isChecked():
            self.compute_preview()

    def refresh_controls(self):
        s = self.state
        count = len(s.elements)
        noun = "Element" if count == 1 else "Elements"
        self.insert_button.setText(f"Insert {count} {noun}" if count else "Insert")
        self.insert_button.setEnabled(bool(s.elements))
        self.whole_box.setEnabled(s.selected is not None)
        self.delete_button.setEnabled(s.selected is not None)
        self.clear_button.setEnabled(bool(s.elements))
        self._refresh_brush()
        self.preview_box.setEnabled(self.fill_box.isChecked())
        model_present = self.extract_runtime is not None and self.extract_runtime.available
        model_busy = "model" in self._jobs
        self.model_row.setVisible(not model_present and not model_busy)
        self.install_button.setVisible(self.extract_runtime is not None)
        self.detect_button.setEnabled(model_present and not model_busy)
        if not model_present:
            if self.extract_runtime is None:
                help_text = ("Choose a Python runtime before using the extraction model. "
                             "Shift-drag a Whole Box, or paint with Keep, without it.")
            else:
                help_text = ("The IS-Net model is not downloaded. Box prediction and Find Elements need it. "
                             "Shift-drag a Whole Box, or paint with Keep, without it.")
            self.model_help.setText(help_text)

        selected = s.selected
        if s.tool == Tool.box:
            hint = ("Drag a box around something to add it. Click an element to select it, Backspace deletes it. "
                    "Shift-drag cuts out the whole box. B / K / R switch tools, Ctrl+Z undoes.")
        elif s.tool == Tool.keep:
            hint = ("Paint over something to make it a new element." if selected is None else
                    f"Paint over parts element {selected+1} should include. Ctrl-click another element to select it.")
        else:
            hint = ("Paint over parts that no element should include." if selected is None else
                    f"Paint over parts element {selected+1} should leave out (they stay in the picture). "
                    "Ctrl-click another element to select it.")
        self.hint.setText(hint)
        self._refresh_work()

    def fill_changed(self,on):
        if not on:
            self._insert_pending = False
            self.preview_box.setChecked(False)
        self.refresh_controls()
        self.canvas.update()

    def preview_changed(self,on):
        if on:
            self.compute_preview()
        self.canvas.update()

    @property
    def showing_preview(self):
        s = self.state
        return self.preview_box.isChecked() and s.filled_for == s.generation and s.filled is not None

    def compute_preview(self):
        s = self.state
        if s.filled_for == s.generation:
            self.canvas.update()
            return
        if (previous := self._jobs.get("preview")) is not None:
            if previous.generation == s.generation:
                return
            del self._jobs["preview"]
            if previous.worker is not None:
                previous.worker.cancel()
        if not s.elements:
            s.accept_preview(s.generation, None)
            self._refresh_work()
            return
        if not self.runtime:
            self._insert_pending = False
            self._status_text = "Choose a Python runtime before computing background fill"
            self._refresh_work()
            return
        job = self._begin_job("preview", "Filling background")
        spawn(self._compute_preview(job))

    def _new_directory(self,label):
        self._serial += 1
        return Path(self._temporary.name)/f"{label}-{self._serial}"

    def _compute_preview(self, job):
        s = self.state
        generation = job.generation
        directory = self._new_directory("fill")
        try:
            if self._jobs.get("preview") is not job or s.generation != generation:
                return
            masks = [e.mask.copy() for e in s.elements]
            image,hole = yield run_blocking(_stage,s.image,masks,directory)
            if self._jobs.get("preview") is not job or s.generation != generation:
                return
            worker = WorkerProcess(self.runtime.python,"image_fill",{
                "image":str(image),"hole":str(hole),"output":str(directory/"filled.png"),"generation":generation,
            },env=self.runtime.child_env())
            result = yield self._track("preview", job, worker)
            filled = yield run_blocking(ImageExtractor.load,result["output"])
            if self._jobs.get("preview") is job and s.accept_preview(generation, filled):
                self._filled_image = _qimage(filled)
                self.canvas.update()
                self._status_text = "Background preview ready"
                if self._insert_pending:
                    self._insert_pending = False
                    self.insert_pressed()
        except Exception as exc:
            if self._jobs.get("preview") is job and s.generation == generation:
                self._status_text = str(exc)
                self._insert_pending = False
        finally:
            self._discard_directory(directory, job.worker)
            self._finish_job("preview", job)

    def _discard_directory(self,directory,worker=None):
        from PySide6.QtCore import QProcess
        if worker and worker.proc and worker.proc.state()!=QProcess.ProcessState.NotRunning:
            QTimer.singleShot(100,lambda:self._discard_directory(directory,worker))
        else:
            run_blocking(shutil.rmtree,directory,True)

    def add_region(self,box,whole_box=False):
        if whole_box:
            self.state.add_region(box)
        else:
            self._infer(box)

    def detect_pressed(self):
        self._infer(None)

    def _infer(self, box, *, initial=False):
        if self.extract_runtime is None or not self.extract_runtime.available:
            return
        if self._closed or "model" in self._jobs:
            return
        text = "Finding the subject inside the box" if box else "Finding the separate subjects"
        job = self._begin_job("model", text)
        spawn(self._infer_task(box, initial, job))

    def _infer_task(self, box, initial, job):
        directory = self._new_directory("model")
        try:
            if self._jobs.get("model") is not job:
                return
            image,_ = yield run_blocking(_stage,self.state.image,[],directory)
            if self._jobs.get("model") is not job or job.generation != self.state.generation:
                return
            xyxy = [box[0],box[1],box[0]+box[2],box[1]+box[3]] if box else None
            worker = self.extract_runtime.inference_worker(image,directory/"masks",xyxy)
            result = yield self._track("model", job, worker)
            masks = yield run_blocking(lambda:[np.asarray(Image.open(p).convert("L")) for p in result["masks"]])
            if self._jobs.get("model") is not job or job.generation != self.state.generation:
                return
            count = self.state.add_masks(masks,region=box,deduplicate=box is None)
            if count and initial:
                self.state.selected = len(self.state.elements)-count
                self.canvas.update()
            self._status_text = (f"Added {count} element{'s' if count != 1 else ''}" if count else
                                 "No new separate elements found. Try Whole Box or Keep.")
        except Exception as exc:
            if self._jobs.get("model") is job:
                self._status_text = str(exc)
        finally:
            self._discard_directory(directory, job.worker)
            self._finish_job("model", job)

    def install_model(self):
        if self._closed or self.extract_runtime is None or self.extract_runtime.available or "model" in self._jobs:
            return
        answer = QMessageBox.question(self,"Download extraction model",DISCLOSURE,
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes or self._closed:
            return
        job = self._begin_job("model", "Downloading the IS-Net model")
        spawn(self._install_model(job))

    def _install_model(self, job):
        completed = False
        try:
            if self._jobs.get("model") is not job:
                return
            worker = self.extract_runtime.install_worker()
            yield self._track("model", job, worker)
            completed = self._jobs.get("model") is job
        except Exception as exc:
            if self._jobs.get("model") is job:
                self._status_text = str(exc)
        finally:
            self._finish_job("model", job)
        if completed and not self._closed:
            self._status_text = "Model downloaded. Elements may differ from Apple Vision."
            self._refresh_work()
            QTimer.singleShot(0, self.detect_pressed)

    def insert_pressed(self):
        if not self.state.elements:
            return
        s = self.state
        if self.fill_box.isChecked() and (s.filled_for != s.generation or s.filled is None):
            self._insert_pending = True
            self.compute_preview()
            return
        self._result = self.Result([e.mask.copy() for e in s.elements],self.fill_box.isChecked(),
            s.filled if s.filled_for == s.generation and self.fill_box.isChecked() else None)
        self.accept()


class ExtractCanvas(QWidget):
    def __init__(self,editor):
        super().__init__(editor)
        self.editor = editor
        self.drag_start = self.drag_now = self.last_paint = self.hover = None
        self.painting = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(300)
        self.setAccessibleName("Extraction canvas")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def picture_rect(self):
        r = QRectF(self.rect()).adjusted(10,10,-10,-10)
        s = max(0, min(r.width()/self.editor.state.w, r.height()/self.editor.state.h))
        w,h = self.editor.state.w*s,self.editor.state.h*s
        return QRectF(r.center().x()-w/2,r.center().y()-h/2,w,h)

    @property
    def scale(self):
        return self.picture_rect.width()/self.editor.state.w

    def to_picture(self,p):
        r = self.picture_rect
        scale = r.width()/self.editor.state.w
        if scale <= 0:
            return (-1, -1)
        return (p.x()-r.x())/scale, (p.y()-r.y())/scale

    def to_view(self,box):
        r = self.picture_rect
        x0,y0,x1,y1 = box
        scale = r.width()/self.editor.state.w
        return QRectF(r.x()+x0*scale, r.y()+y0*scale, (x1-x0)*scale, (y1-y0)*scale)

    def paintEvent(self, event):
        e, s = self.editor, self.editor.state
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        canvas_clip = QPainterPath()
        canvas_clip.addRoundedRect(QRectF(self.rect()), 6, 6)
        p.fillPath(canvas_clip, Theme.canvas_bottom())
        p.setClipPath(canvas_clip)

        r = self.picture_rect
        if r.isEmpty():
            return
        preview = e.showing_preview
        p.save()
        p.setClipRect(r, Qt.ClipOperation.IntersectClip)
        p.fillRect(r, QColor("#525252"))
        tile = QColor("#666666")
        rows, cols = math.ceil(r.height()/10), math.ceil(r.width()/10)
        for y in range(rows):
            for x in range(y % 2, cols, 2):
                p.fillRect(QRectF(r.x()+x*10, r.y()+y*10, 10, 10), tile)
        p.drawImage(r, e._filled_image if preview else e._image)
        p.restore()

        p.setFont(Theme.font(11, QFont.Weight.Bold))
        metrics = p.fontMetrics()
        for i, element in enumerate(s.elements):
            color = QColor(e.colors[element.color % len(e.colors)])
            selected = s.selected == i
            if not preview:
                if i not in e._overlays:
                    rgba = np.empty((s.h,s.w,4),np.uint8)
                    rgba[:,:,:3] = color.red(),color.green(),color.blue()
                    rgba[:,:,3] = (element.mask.astype(np.uint16)*140//255).astype(np.uint8)
                    e._overlays[i] = QImage(rgba.data,s.w,s.h,rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
                p.setOpacity(1 if selected else 0.55)
                p.drawImage(r, e._overlays[i])
                p.setOpacity(1)
            if element.bounds:
                box = self.to_view(element.bounds).adjusted(-1, -1, 1, 1)
                edge = QColor(color)
                edge.setAlphaF(1 if selected else 0.8)
                pen = QPen(edge, 2 if selected else 1)
                if not selected:
                    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                    pen.setDashPattern([5, 3])
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(box)

                label = str(i+1)
                badge = QRectF(box.x(), box.y(), metrics.horizontalAdvance(label)+10, metrics.height()+2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                p.drawRoundedRect(badge, 4, 4)
                p.setPen(Qt.GlobalColor.white)
                p.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

        p.setBrush(Qt.BrushStyle.NoBrush)
        if s.tool == Tool.box and self.drag_start is not None and not self.painting:
            box = QRectF(self.drag_start, self.drag_now).normalized()
            p.fillRect(box, QColor.fromRgbF(1, 1, 1, 0.08))
            pen = QPen(Qt.GlobalColor.white, 1.5)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            pen.setDashPattern([6/1.5, 4/1.5])  # Qt expresses dash lengths in pen widths.
            p.setPen(pen)
            p.drawRect(box)
        elif self.hover is not None and s.tool != Tool.box:
            dark = is_dark()
            if s.tool == Tool.keep:
                color = QColor("#30d158" if dark else "#34c759")
            else:
                color = QColor("#ff453a" if dark else "#ff3b30")
            p.setPen(QPen(color, 1.5))
            p.drawEllipse(self.hover, s.brush, s.brush)

    def refresh_cursor(self):
        crosshair = (self.editor.state.tool == Tool.box and self.hover is not None
                     and self.picture_rect.contains(self.hover))
        shape = Qt.CursorShape.CrossCursor if crosshair else Qt.CursorShape.ArrowCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def enterEvent(self, event):
        self.hover = event.position()
        self.refresh_cursor()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_cursor()

    def mousePressEvent(self,event):
        if event.button() != Qt.MouseButton.LeftButton or self.picture_rect.isEmpty():
            return
        self.setFocus()
        s = self.editor.state
        p = event.position()
        self.drag_start = self.drag_now = p
        if s.tool != Tool.box and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            s.selected = s.element(self.to_picture(p))
            self.drag_start = None
            self.editor.refresh_controls()
        elif s.tool != Tool.box:
            self.painting = True
            s.begin_stroke()
            self.last_paint = self.to_picture(p)
            s.paint(self.last_paint,self.last_paint,s.brush/self.scale)
            self.editor._overlays.clear()
            self.editor.refresh_controls()
        self.update()

    def mouseMoveEvent(self,event):
        self.hover = event.position()
        self.refresh_cursor()
        if self.drag_start is not None:
            self.drag_now = self.hover
            if self.painting:
                q = self.to_picture(self.hover)
                s = self.editor.state
                s.paint(self.last_paint,q,s.brush/self.scale)
                self.last_paint = q
                self.editor._overlays.clear()
        self.update()

    def mouseReleaseEvent(self,event):
        if event.button() != Qt.MouseButton.LeftButton or self.drag_start is None:
            return
        a,b = self.drag_start,event.position()
        self.drag_start = self.drag_now = None
        s = self.editor.state
        if self.painting:
            self.painting = False
            self.last_paint = None
            s.end_stroke()
        elif abs(a.x()-b.x())<4 and abs(a.y()-b.y())<4:
            s.selected = s.element(self.to_picture(b))
            self.editor.refresh_controls()
        else:
            pa,pb = self.to_picture(a),self.to_picture(b)
            x0,y0 = max(0,min(pa[0],pb[0])),max(0,min(pa[1],pb[1]))
            x1,y1 = min(s.w,max(pa[0],pb[0])),min(s.h,max(pa[1],pb[1]))
            if x1-x0>=3 and y1-y0>=3:
                self.editor.add_region((x0,y0,x1-x0,y1-y0),bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        self.update()

    def leaveEvent(self,event):
        self.hover = None
        self.refresh_cursor()
        self.update()

    def keyPressEvent(self,event):
        s = self.editor.state
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if s.selected is None:
                QApplication.beep()
            else:
                s.delete_selected()
        elif event.key() == Qt.Key.Key_Escape and s.selected is not None:
            s.selected = None
            self.editor.refresh_controls()
        elif event.text().lower() in ("b","k","r"):
            self.editor.set_tool({"b":Tool.box,"k":Tool.keep,"r":Tool.remove}[event.text().lower()])
        elif event.text() in ("[","]"):
            self.editor.nudge_brush(-3 if event.text()=="[" else 3)
        else:
            super().keyPressEvent(event)
            return
        self.update()


class ExtractMixin:
    def extract_elements_pressed(self):
        spawn(self.extract_elements(interactive=True))

    def extract_elements(self,interactive=True):
        if getattr(self,"_extract_editor",None) is not None:
            return
        oid,o,name = self.selection,self.obj_info,self.scene_name
        stops = (self.scene or {}).get("stops",[])
        stop = self.stop_index
        box = stop_box(stops[stop] if stop<len(stops) else None,oid) if oid is not None else None
        deck = self.work_deck
        if oid is None or not o or o.get("kind")!="image" or not o.get("imagePath") or box is None or not deck or not name:
            self.toast("Select a picture on the slide first",error=True)
            return
        path = o["imagePath"]
        if Path(path).suffix.lower()==".svg":
            self.toast("Extract Elements works on bitmap pictures, not vector ones",error=True)
            return
        context = self.edit_context
        first_shown = next((i for i,s in enumerate(stops) if any(int(v[0])==oid for v in s.get("visible",[]))),stop)
        try:
            yield from self.finish_edits()
            model = ExtractRuntime(self.paths, self.runtime)
            source = o.get("image") or "the picture"
            loading = f"Finding the elements of {source}…" if model.available else f"Loading {source}…"
            self.busy_pill.show(loading, spinning=True, cancellable=False)
            try:
                image = yield run_blocking(ImageExtractor.load, path)
            except Exception as exc:
                self.alert("Could not analyse the picture", str(exc))
                return
            finally:
                self.update_busy_pill()
            if interactive:
                editor = ExtractEditor(image, source=source, runtime=self.runtime,
                                       extract_runtime=model, parent=self.window)
                self._extract_editor = editor
                future = Future()
                editor.run(done=future.set_result)
                if model.available:
                    QTimer.singleShot(0, lambda: editor._infer(None, initial=True))
                choice = yield future
            else:
                # Noninteractive callers must explicitly request model inference;
                # absence/error is never turned into a fabricated whole-box mask.
                if not model.available:
                    raise RuntimeError("Download the extraction model first; manual extraction remains available")
                with tempfile.TemporaryDirectory(prefix="manimble-extract-auto-") as directory:
                    image_file,_ = yield run_blocking(_stage,image,[],directory)
                    worker = model.inference_worker(image_file,Path(directory)/"masks")
                    self._extract_worker = worker
                    result = yield worker.start()
                    masks = yield run_blocking(lambda:[np.asarray(Image.open(p).convert("L")) for p in result["masks"]])
                if not masks:
                    self.toast("No separate elements were found in this picture", error=True)
                    return
                choice = ExtractEditor.Result(masks,True,None)
            if choice is None or self.work_deck!=deck or self.scene_name!=name:
                return
            cutting = ("Cutting out the elements and filling in the background…" if choice.fill else
                       "Cutting out the elements…")
            self.busy_pill.show(cutting, spinning=True, cancellable=False)
            try:
                filled = choice.filled
                if choice.fill and filled is None:
                    try:
                        with tempfile.TemporaryDirectory(prefix="manimble-extract-fill-") as directory:
                            image_file, hole_file = yield run_blocking(_stage, image, choice.masks, directory)
                            worker = WorkerProcess(self.runtime.python, "image_fill", {
                                "image": str(image_file), "hole": str(hole_file), "output": str(Path(directory)/"filled.png"),
                            }, env=self.runtime.child_env())
                            self._extract_worker = worker
                            result = yield worker.start()
                            filled = yield run_blocking(ImageExtractor.load, result["output"])
                    except Exception as exc:
                        self.alert("Could not fill in the background", str(exc))
                        return
                if self.work_deck != deck or self.scene_name != name:
                    return
                parts, background = yield run_blocking(save_parts, image, choice.masks, deck,
                                                       Path(path).stem, filled if choice.fill else None)
            except _NothingToExtract:
                self.toast("Nothing to extract", error=True)
                return
            except _SaveExtractionError as exc:
                self.alert(f"Could not save {exc.filename}", str(exc))
                return
            finally:
                self.update_busy_pill()
            request = build_insert_request(name,stop,first_shown,oid,box,image.size,parts,background,context.seen)
            def commit():
                if self.work_deck!=deck:
                    return
                self.busy += 1
                try:
                    reply = yield self.call("insert_many", request)
                finally:
                    self.busy -= 1
                if reply.get("ok") is False:
                    self.toast(reply.get("error") or "could not insert the elements", error=True)
                    return
                yield from self.load_scene(name,keep_stop=True)
                yield from self.reload_state()
                count = len(parts)
                what = f"{count} element{'s' if count != 1 else ''}"
                self.toast(f"Inserted {what} and filled in the picture behind them · Ctrl+Z undoes it all"
                           if background else f"Inserted {what} from the picture · Ctrl+Z undoes them all")
            yield self.submit(commit)
        except Exception as exc:
            self.alert("Could not extract elements",str(exc))
        finally:
            self._extract_worker = None
            self._extract_editor = None

    def cancel_extraction(self):
        editor = getattr(self,"_extract_editor",None)
        if editor:
            editor.reject()
        worker = getattr(self,"_extract_worker",None)
        if worker:
            worker.cancel()
