"""Manual/automatic picture extraction, local mask history, and one engine undo unit."""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
import tempfile
import shutil

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSlider, QVBoxLayout, QWidget)

from .extract_runtime import DISCLOSURE, ExtractRuntime
from .models import Box, stop_box
from .tasks import spawn
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
        created.append(deck/file)
        with stream:
            img.save(stream,format="PNG")
        return file
    try:
        parts = []
        for mask in masks:
            part = ImageExtractor.cut_out(image,mask)
            if part:
                parts.append((save(part.image,"part"),part.box))
        if not parts:
            raise ValueError("Nothing to extract")
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


class ExtractEditor(QDialog):
    Tool = Tool
    colors = ("#7D5CFF","#33C759","#FF992E","#4DB3FF","#FF458C","#F2D91A")

    @dataclass
    class Result:
        masks: list
        fill: bool
        filled: Image.Image | None

    def __init__(self,image,masks=(),source="the picture",runtime=None,extract_runtime=None,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Extract Elements")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(960,680)
        self.setMinimumSize(720,520)
        self.state = ExtractionState(image,masks)
        self.runtime,self.extract_runtime = runtime,extract_runtime
        self.state.on_change = self.changed
        self._temporary = tempfile.TemporaryDirectory(prefix="manimble-extract-")
        self._workers = set()
        self._preview_worker = None
        self._model_worker = None
        self._closed = False
        self._operations = 0
        self._operation_epoch = 0
        self._insert_pending = False
        self._serial = 0
        self._finish = None
        self._result = None
        self._overlays = {}
        self._image = _qimage(self.state.image)
        self._filled_image = None
        self._last_preview_generation = -1
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Extract elements from {source}"))
        row = QHBoxLayout()
        self.tool_control = QButtonGroup(self)
        for tool in Tool:
            button = QPushButton(tool.value)
            button.setCheckable(True)
            button.setChecked(tool == Tool.box)
            self.tool_control.addButton(button)
            button.clicked.connect(lambda checked,t=tool:self.set_tool(t))
            row.addWidget(button)
        row.addWidget(QLabel("Brush"))
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(3,60)
        self.brush_slider.setValue(14)
        self.brush_slider.setMaximumWidth(110)
        self.brush_slider.valueChanged.connect(self.brush_changed)
        row.addWidget(self.brush_slider)
        row.addStretch()
        self.detect_button = QPushButton("Find Elements")
        self.detect_button.clicked.connect(self.detect_pressed)
        row.addWidget(self.detect_button)
        self.whole_box = QPushButton("Whole Box")
        self.whole_box.clicked.connect(self.state.whole_box_pressed)
        row.addWidget(self.whole_box)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.state.delete_selected)
        row.addWidget(self.delete_button)
        self.clear_button = QPushButton("Clear All")
        self.clear_button.clicked.connect(self.state.clear_pressed)
        row.addWidget(self.clear_button)
        layout.addLayout(row)
        self.canvas = ExtractCanvas(self)
        layout.addWidget(self.canvas,1)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        progress_row = QHBoxLayout()
        self.install_button = QPushButton("Install extraction model…")
        self.install_button.clicked.connect(self.install_model)
        progress_row.addWidget(self.install_button)
        self.progress = QProgressBar()
        self.progress.setRange(0,100)
        self.progress.hide()
        progress_row.addWidget(self.progress,1)
        self.cancel_work = QPushButton("Cancel operation")
        self.cancel_work.clicked.connect(self.cancel_operation)
        self.cancel_work.hide()
        progress_row.addWidget(self.cancel_work)
        layout.addLayout(progress_row)
        row = QHBoxLayout()
        self.fill_box = QCheckBox("Fill in the background behind them")
        self.fill_box.setChecked(True)
        self.fill_box.toggled.connect(self.fill_changed)
        row.addWidget(self.fill_box)
        self.preview_box = QCheckBox("Preview background")
        self.preview_box.toggled.connect(self.preview_changed)
        row.addWidget(self.preview_box)
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.insert_button = QPushButton("Insert")
        self.insert_button.setDefault(True)
        self.insert_button.clicked.connect(self.insert_pressed)
        row.addWidget(self.insert_button)
        layout.addLayout(row)
        self._shortcuts = []
        for key,fn in (("Ctrl+Z",self.state.undo),("Ctrl+Shift+Z",self.state.redo),("Ctrl+Y",self.state.redo)):
            shortcut = QShortcut(QKeySequence(key),self)
            shortcut.activated.connect(fn)
            self._shortcuts.append(shortcut)
        self.finished.connect(self._finished)
        self.refresh_controls()

    def run(self,on=None,done=None):
        if on is not None:
            self.setParent(on,self.windowFlags())
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
        self._operation_epoch += 1
        self._insert_pending = False
        self._serial += 1
        for worker in tuple(self._workers):
            worker.cancel()
        self._model_worker,self._preview_worker = None,None
        self.progress.hide()
        self.cancel_work.hide()
        if not self._closed:
            self.status.setText("Operation cancelled; manual tools remain available.")
            self.refresh_controls()

    def _track(self,worker):
        self._workers.add(worker)
        worker.progress.connect(lambda x,text:(self.progress.setValue(round(x*100)),self.status.setText(text)))
        self.progress.show()
        self.cancel_work.show()
        future = worker.start()
        worker.proc.finished.connect(lambda *_:self._workers.discard(worker))
        return future

    def set_tool(self,tool):
        self.state.tool = tool
        for button in self.tool_control.buttons():
            button.setChecked(button.text()==tool.value)
        self.refresh_controls()
        self.canvas.update()

    def brush_changed(self,value):
        self.state.brush = value
        self.canvas.update()

    def nudge_brush(self,delta):
        self.brush_slider.setValue(max(3,min(60,self.brush_slider.value()+delta)))

    def changed(self):
        self._insert_pending = False
        self._overlays.clear()
        self.canvas.update()
        self.refresh_controls()
        if self.preview_box.isChecked():
            self.compute_preview()

    def refresh_controls(self):
        s = self.state
        self.insert_button.setText(f"Insert {len(s.elements)} Elements" if s.elements else "Insert")
        self.insert_button.setEnabled(bool(s.elements))
        self.whole_box.setEnabled(s.selected is not None)
        self.delete_button.setEnabled(s.selected is not None)
        self.clear_button.setEnabled(bool(s.elements))
        self.brush_slider.setEnabled(s.tool != Tool.box)
        self.preview_box.setEnabled(self.fill_box.isChecked())
        self.install_button.setVisible(self.extract_runtime is not None and not self.extract_runtime.available)
        self.install_button.setEnabled(self._model_worker is None)
        self.detect_button.setEnabled(self.extract_runtime is not None and self._model_worker is None)
        hints = {Tool.box:"Drag a box; click an element to select it. Shift-drag makes a Whole Box without the model.",
            Tool.keep:"Paint to include pixels; with no selection, Keep creates an element. Ctrl-click selects another element.",
            Tool.remove:"Paint to remove pixels from the selected element, or all elements when none is selected."}
        self.hint.setText(hints[s.tool]+" B / K / R: tools; [ / ]: radius; Delete: remove; Ctrl+Z: undo; Escape: clear selection.")

    def fill_changed(self,on):
        if not on:
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
        if self.state.filled_for == self.state.generation:
            self.canvas.update()
            return
        if self._preview_worker is not None:
            self._preview_worker.cancel()
        spawn(self._compute_preview())

    def _new_directory(self,label):
        self._serial += 1
        return Path(self._temporary.name)/f"{label}-{self._serial}"

    def _compute_preview(self):
        if not self.runtime:
            self.status.setText("Choose a Python runtime before computing background fill")
            return
        epoch = self._operation_epoch
        s = self.state
        generation = s.generation
        masks = [e.mask.copy() for e in s.elements]
        if not masks:
            s.accept_preview(generation,None)
            return
        self._operations += 1
        directory = self._new_directory("fill")
        try:
            image,hole = yield run_blocking(_stage,s.image,masks,directory)
            if self._closed or s.generation != generation or epoch != self._operation_epoch:
                return
            worker = WorkerProcess(self.runtime.python,"image_fill",{
                "image":str(image),"hole":str(hole),"output":str(directory/"filled.png"),"generation":generation,
            },env=self.runtime.child_env())
            self._preview_worker = worker
            result = yield self._track(worker)
            filled = yield run_blocking(ImageExtractor.load,result["output"])
            if not self._closed and epoch == self._operation_epoch and s.accept_preview(generation,filled):
                self._filled_image = _qimage(filled)
                self._last_preview_generation = generation
                self.canvas.update()
                self.status.setText("Background preview ready")
                if self._insert_pending:
                    self._insert_pending = False
                    self.insert_pressed()
        except Exception as exc:
            if not self._closed and s.generation == generation:
                self.status.setText(str(exc))
                self._insert_pending = False
        finally:
            self._operations -= 1
            self._discard_directory(directory,locals().get("worker"))
            if self._preview_worker is locals().get("worker"):
                self._preview_worker = None
            if self._preview_worker is None:
                self.progress.hide()
                self.cancel_work.hide()

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

    def _infer(self,box):
        if self.extract_runtime is None or not self.extract_runtime.available:
            self.status.setText("Install extraction model for Box prediction / Find Elements. Shift-drag a Whole Box, or paint with Keep, without it.")
            return
        if self._model_worker is None:
            spawn(self._infer_task(box))

    def _infer_task(self,box):
        self._operations += 1
        epoch = self._operation_epoch
        directory = self._new_directory("model")
        generation = self.state.generation
        try:
            image,_ = yield run_blocking(_stage,self.state.image,[],directory)
            if self._closed or epoch != self._operation_epoch:
                return
            xyxy = [box[0],box[1],box[0]+box[2],box[1]+box[3]] if box else None
            worker = self.extract_runtime.inference_worker(image,directory/"masks",xyxy)
            self._model_worker = worker
            self.refresh_controls()
            result = yield self._track(worker)
            masks = yield run_blocking(lambda:[np.asarray(Image.open(p).convert("L")) for p in result["masks"]])
            if self._closed or epoch != self._operation_epoch or generation != self.state.generation:
                return
            count = self.state.add_masks(masks,region=box,deduplicate=box is None)
            self.status.setText(f"Added {count} editable candidates" if count else "No new separate elements found. Try Whole Box or Keep.")
        except Exception as exc:
            if not self._closed:
                self.status.setText(str(exc))
        finally:
            self._operations -= 1
            self._discard_directory(directory,locals().get("worker"))
            if self._model_worker is locals().get("worker"):
                self._model_worker = None
                self.progress.hide()
                self.cancel_work.hide()
                self.refresh_controls()

    def install_model(self):
        if self.extract_runtime is None or self._model_worker is not None:
            return
        answer = QMessageBox.question(self,"Install extraction model",DISCLOSURE,
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        spawn(self._install_model())

    def _install_model(self):
        python = self.runtime.python
        while not self._closed:
            try:
                worker = self.extract_runtime.install_worker(python)
                self._model_worker = worker
                self.refresh_controls()
                yield self._track(worker)
                self.status.setText("Model installed. Find Elements returns editable candidates; segments may differ from Apple Vision.")
                return
            except Exception as exc:
                self.status.setText(str(exc))
                if "choose another installed interpreter" not in str(exc):
                    return
                python,_ = QFileDialog.getOpenFileName(self,"Choose installed Python 3.11–3.14 (not 3.14.1)",str(Path(python).parent))
                if not python:
                    return
            finally:
                self._model_worker = None
                self.progress.hide()
                self.cancel_work.hide()
                self.refresh_controls()

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
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.drag_start = self.drag_now = self.last_paint = self.hover = None
        self.painting = False

    @property
    def picture_rect(self):
        r = QRectF(self.rect()).adjusted(10,10,-10,-10)
        s = min(r.width()/self.editor.state.w,r.height()/self.editor.state.h)
        w,h = self.editor.state.w*s,self.editor.state.h*s
        return QRectF(r.center().x()-w/2,r.center().y()-h/2,w,h)

    @property
    def scale(self):
        return self.picture_rect.width()/self.editor.state.w

    def to_picture(self,p):
        r = self.picture_rect
        return (p.x()-r.x())/self.scale,(p.y()-r.y())/self.scale

    def to_view(self,box):
        r = self.picture_rect
        x0,y0,x1,y1 = box
        return QRectF(r.x()+x0*self.scale,r.y()+y0*self.scale,(x1-x0)*self.scale,(y1-y0)*self.scale)

    def paintEvent(self,event):
        e,s = self.editor,self.editor.state
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = self.picture_rect
        p.fillRect(self.rect(),QColor("#292929"))
        p.save()
        p.setClipRect(r)
        p.fillRect(r,QColor("#525252"))
        for y in range(math.ceil(r.height()/10)):
            for x in range(y%2,math.ceil(r.width()/10),2):
                p.fillRect(QRectF(r.x()+x*10,r.y()+y*10,10,10),QColor("#666666"))
        p.drawImage(r,e._filled_image if e.showing_preview else e._image)
        p.restore()
        for i,element in enumerate(s.elements):
            color = QColor(e.colors[element.color%len(e.colors)])
            if not e.showing_preview:
                if i not in e._overlays:
                    rgba = np.empty((s.h,s.w,4),np.uint8)
                    rgba[:,:,:3] = color.red(),color.green(),color.blue()
                    rgba[:,:,3] = (element.mask.astype(np.uint16)*140//255).astype(np.uint8)
                    e._overlays[i] = QImage(rgba.data,s.w,s.h,rgba.strides[0],QImage.Format.Format_RGBA8888).copy()
                p.setOpacity(1 if s.selected==i else 0.55)
                p.drawImage(r,e._overlays[i])
                p.setOpacity(1)
            if element.bounds:
                box = self.to_view(element.bounds).adjusted(-1,-1,1,1)
                p.setPen(QPen(color,2 if s.selected==i else 1,Qt.PenStyle.SolidLine if s.selected==i else Qt.PenStyle.DashLine))
                p.drawRect(box)
                badge = QRectF(box.x(),box.y(),24,18)
                p.fillRect(badge,color)
                p.setPen(Qt.GlobalColor.white)
                p.drawText(badge,Qt.AlignmentFlag.AlignCenter,str(i+1))
        if self.drag_start is not None and not self.painting:
            p.setPen(QPen(Qt.GlobalColor.white,1.5,Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self.drag_start,self.drag_now).normalized())
        if self.hover is not None and s.tool!=Tool.box:
            p.setPen(QPen(QColor("#33CC55" if s.tool==Tool.keep else "#FF5555"),1.5))
            p.drawEllipse(self.hover,s.brush,s.brush)

    def mousePressEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton or not self.picture_rect.contains(event.position()):
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
        self.update()

    def mouseMoveEvent(self,event):
        self.hover = event.position()
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
        if self.drag_start is None:
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
        self.update()

    def keyPressEvent(self,event):
        s = self.editor.state
        if event.key() in (Qt.Key.Key_Delete,Qt.Key.Key_Backspace):
            s.delete_selected()
        elif event.key()==Qt.Key.Key_Escape:
            s.selected = None
            self.editor.refresh_controls()
        elif event.text().lower() in ("b","k","r"):
            self.editor.set_tool({"b":Tool.box,"k":Tool.keep,"r":Tool.remove}[event.text().lower()])
        elif event.text() in ("[","]"):
            self.editor.nudge_brush(-3 if event.text()=="[" else 3)
        else:
            super().keyPressEvent(event)
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
            image = yield run_blocking(ImageExtractor.load,path)
            model = ExtractRuntime(self.paths,self.runtime)
            if interactive:
                editor = ExtractEditor(image,source=o.get("image","the picture"),runtime=self.runtime,extract_runtime=model,parent=self.window)
                self._extract_editor = editor
                future = Future()
                editor.run(done=future.set_result)
                if model.available:
                    QTimer.singleShot(0,editor.detect_pressed)
                choice = yield future
            else:
                # Noninteractive callers must explicitly request model inference;
                # absence/error is never turned into a fabricated whole-box mask.
                if not model.available:
                    raise RuntimeError("Install extraction model first; manual extraction remains available")
                with tempfile.TemporaryDirectory(prefix="manimble-extract-auto-") as directory:
                    image_file,_ = yield run_blocking(_stage,image,[],directory)
                    worker = model.inference_worker(image_file,Path(directory)/"masks")
                    self._extract_worker = worker
                    result = yield worker.start()
                    masks = yield run_blocking(lambda:[np.asarray(Image.open(p).convert("L")) for p in result["masks"]])
                if not masks:
                    raise ValueError("No separate elements found; use Whole Box or Keep")
                choice = ExtractEditor.Result(masks,True,None)
            if choice is None or self.work_deck!=deck or self.scene_name!=name:
                return
            filled = choice.filled
            if choice.fill and filled is None:
                with tempfile.TemporaryDirectory(prefix="manimble-extract-fill-") as directory:
                    image_file,hole_file = yield run_blocking(_stage,image,choice.masks,directory)
                    worker = WorkerProcess(self.runtime.python,"image_fill",{
                        "image":str(image_file),"hole":str(hole_file),"output":str(Path(directory)/"filled.png"),
                    },env=self.runtime.child_env())
                    # Retain the cancellable process on the controller until it finishes.
                    self._extract_worker = worker
                    result = yield worker.start()
                    filled = yield run_blocking(ImageExtractor.load,result["output"])
            if self.work_deck!=deck or self.scene_name!=name:
                return
            parts,background = yield run_blocking(save_parts,image,choice.masks,deck,Path(path).stem,filled if choice.fill else None)
            request = build_insert_request(name,stop,first_shown,oid,box,image.size,parts,background,context.seen)
            def commit():
                if self.work_deck!=deck:
                    return
                reply = yield self.call("insert_many",request)
                if reply.get("ok") is False:
                    raise RuntimeError(reply.get("error","Could not insert extracted elements"))
                yield from self.load_scene(name,keep_stop=True)
                yield from self.reload_state()
                self.toast(f"Inserted {len(parts)} elements"+(" and filled the background" if background else "")+" · Ctrl+Z undoes it all")
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
