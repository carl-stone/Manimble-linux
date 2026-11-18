"""IS-Net subject-finding worker (onnxruntime, CPU). No Qt imports on this boundary.

IS-Net returns one soft foreground mask for the whole picture. Each connected piece of it becomes an element, the
Linux counterpart of the separate instances Apple Vision returns; specks are dropped. Box selection follows
ImageExtractor.swift: look at a crop around the box so a smaller thing becomes the subject, keep the pieces mostly
inside the box, and clip to it.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .extract_runtime import verify_model

SIZE = 1024             # IS-Net's input side
SPECK = 1/50000         # pieces smaller than this fraction of the analysed picture are noise
MIN_SPECK = 16          # ... and never fewer pixels than this
FRINGE = 2              # soft edge pixels kept around each piece, for anti-aliased cutouts
_EIGHT = np.ones((3,3),bool)


def soft_mask(session, image):
    """One 0–255 foreground mask at the picture's size; same pre/postprocessing as rembg's IS-Net session."""
    x = np.asarray(image.convert("RGB").resize((SIZE,SIZE),Image.Resampling.LANCZOS),np.float32)
    x = x / max(float(x.max()),1e-6) - 0.5
    name = session.get_inputs()[0].name
    pred = session.run(None,{name:x.transpose(2,0,1)[None]})[0][0,0]
    lo,hi = float(pred.min()),float(pred.max())
    pred = (pred-lo)/(hi-lo) if hi > lo else np.zeros_like(pred)
    mask = Image.fromarray((pred*255).astype(np.uint8)).resize(image.size,Image.Resampling.LANCZOS)
    return np.asarray(mask)


def pieces(soft):
    """Yield one full-size 0–255 mask per connected piece of ``soft``, specks excluded."""
    core = soft > 127
    labels,count = ndimage.label(core,structure=_EIGHT)
    if not count:
        return
    sizes = np.bincount(labels.ravel())
    least = max(MIN_SPECK,soft.size*SPECK)
    h,w = soft.shape
    for i,found in enumerate(ndimage.find_objects(labels),start=1):
        if found is None or sizes[i] < least:
            continue
        ys = slice(max(0,found[0].start-FRINGE),min(h,found[0].stop+FRINGE))
        xs = slice(max(0,found[1].start-FRINGE),min(w,found[1].stop+FRINGE))
        own = labels[ys,xs] == i
        # The fringe takes soft edge values but never another piece's pixels.
        near = ndimage.binary_dilation(own,structure=_EIGHT,iterations=FRINGE) & ~(core[ys,xs] & ~own)
        mask = np.zeros((h,w),np.uint8)
        mask[ys,xs] = np.where(near,soft[ys,xs],0)
        yield mask


def in_box(soft, box):
    """The subject inside ``box`` (pixels of ``soft``'s crop): ImageExtractor.swift's selection rules.

    Pieces at least half inside the box are kept; otherwise the piece with the most area inside, if that covers more
    than a tenth of the box. The result is clipped to the box. ``None`` when nothing qualifies.
    """
    bx0,by0,bx1,by1 = box
    chosen,fallback,best = [],None,0
    for mask in pieces(soft):
        core = mask > 127
        total = int(core.sum())
        inside = int(core[by0:by1,bx0:bx1].sum())
        if inside*2 >= total:
            chosen.append(mask)
        elif inside > best:
            fallback,best = mask,inside
    if not chosen and fallback is not None and best > 0.1*(bx1-bx0)*(by1-by0):
        chosen = [fallback]
    if not chosen:
        return None
    out = np.zeros(soft.shape,np.uint8)
    out[by0:by1,bx0:bx1] = np.maximum.reduce([m[by0:by1,bx0:bx1] for m in chosen])
    return out


def run(job, progress):
    model = verify_model(job["model"])
    import onnxruntime
    progress(0.05,"Loading the IS-Net model")
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3
    session = onnxruntime.InferenceSession(str(model),options,providers=["CPUExecutionProvider"])
    image = Image.open(job["image"]).convert("RGB")
    w,h = image.size
    directory = Path(job["directory"])
    directory.mkdir(parents=True,exist_ok=True)
    files = []

    def save(mask):
        path = directory / f"mask-{len(files)}.png"
        temporary = path.with_suffix(".tmp.png")
        Image.fromarray(mask).save(temporary)
        temporary.replace(path)
        files.append(str(path))

    if job.get("box") is None:
        progress(0.2,"Finding the separate subjects")
        for mask in pieces(soft_mask(session,image)):
            save(mask)
    else:
        x0,y0,x1,y1 = map(float,job["box"])
        bx0,by0 = max(0,math.floor(x0)),max(0,math.floor(y0))
        bx1,by1 = min(w,math.ceil(x1)),min(h,math.ceil(y1))
        if bx1-bx0 < 2 or by1-by0 < 2:
            raise ValueError("The extraction box is outside the picture")
        pad = max(bx1-bx0,by1-by0)*0.12
        cx0,cy0 = max(0,math.floor(bx0-pad)),max(0,math.floor(by0-pad))
        cx1,cy1 = min(w,math.ceil(bx1+pad)),min(h,math.ceil(by1+pad))
        progress(0.2,"Finding the subject inside the box")
        found = in_box(soft_mask(session,image.crop((cx0,cy0,cx1,cy1))),(bx0-cx0,by0-cy0,bx1-cx0,by1-cy0))
        if found is not None:
            mask = np.zeros((h,w),np.uint8)
            mask[cy0:cy1,cx0:cx1] = found
            save(mask)
    progress(1,"Elements ready" if files else "No separate elements found; try Whole Box or Keep")
    return {"masks":files,"count":len(files)}


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(run)
