"""Deterministic, straight-alpha RGBA PatchMatch, ported from ImageFill.swift.

The GUI runs ``run`` in a WorkerProcess; the array API is also useful headlessly.
Only the bytes covered by the supplied hole are ever published back to the image.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from numba import njit
from PIL import Image


class FillError(ValueError):
    pass


@njit(cache=True)
def _next(state):
    state = state + np.uint64(0x9E3779B97F4A7C15)
    z = state
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return state, z ^ (z >> np.uint64(31))


@njit(cache=True)
def _seed(seed):
    return np.uint64(seed) * np.uint64(0x9E3779B97F4A7C15)


@njit(cache=True)
def _marks(hole, radius):
    h, w = hole.shape
    integral = np.zeros((h + 1, w + 1), np.int64)
    for y in range(h):
        row = 0
        for x in range(w):
            row += int(hole[y, x])
            integral[y + 1, x + 1] = integral[y, x + 1] + row
    targets = np.zeros((h, w), np.bool_)
    valid = np.zeros((h, w), np.bool_)
    for y in range(h):
        y0, y1 = max(0, y-radius), min(h, y+radius+1)
        for x in range(w):
            x0, x1 = max(0, x-radius), min(w, x+radius+1)
            n = integral[y1, x1]-integral[y0, x1]-integral[y1, x0]+integral[y0, x0]
            targets[y, x] = n > 0
            valid[y, x] = n == 0 and x >= radius and y >= radius and x < w-radius and y < h-radius
    return np.flatnonzero(targets), np.flatnonzero(valid), valid.ravel()


@njit(cache=True)
def _downsample(px, hole):
    h, w = hole.shape
    oh, ow = (h+1)//2, (w+1)//2
    out = np.zeros((oh, ow, 4), np.float32)
    mask = np.zeros((oh, ow), np.bool_)
    for y in range(oh):
        for x in range(ow):
            n = 0
            for dy in range(2):
                for dx in range(2):
                    sy, sx = min(h-1, 2*y+dy), min(w-1, 2*x+dx)
                    if hole[sy, sx]:
                        mask[y, x] = True
                    else:
                        out[y, x] += px[sy, sx]
                        n += 1
            if n:
                out[y, x] /= np.float32(n)
    return out, mask


@njit(cache=True)
def _diffuse(px, hole):
    h, w = hole.shape
    out, known = px.copy(), ~hole
    while not np.all(known):
        newly = known.copy()
        count = 0
        for y in range(h):
            for x in range(w):
                if known[y, x]:
                    continue
                acc = np.zeros(4, np.float32)
                n = 0
                for dx, dy in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(1,1),(-1,1),(1,-1)):
                    nx, ny = x+dx, y+dy
                    if 0 <= nx < w and 0 <= ny < h and known[ny, nx]:
                        acc += out[ny, nx]
                        n += 1
                if n:
                    out[y, x] = acc / np.float32(n)
                    newly[y, x] = True
                    count += 1
        if not count:
            break
        known = newly
    return out


@njit(cache=True)
def _upsample(coarse, px, hole):
    ch, cw = coarse.shape[:2]
    h, w = hole.shape
    for y in range(h):
        fy = np.float32(max(0, min(ch-1, (y+0.5)/2-0.5)))
        y0 = int(fy)
        y1, ty = min(ch-1, y0+1), fy-y0
        for x in range(w):
            if not hole[y, x]:
                continue
            fx = np.float32(max(0, min(cw-1, (x+0.5)/2-0.5)))
            x0 = int(fx)
            x1, tx = min(cw-1, x0+1), fx-x0
            for c in range(4):
                a = coarse[y0,x0,c]*(1-tx)+coarse[y0,x1,c]*tx
                b = coarse[y1,x0,c]*(1-tx)+coarse[y1,x1,c]*tx
                px[y,x,c] = a*(1-ty)+b*ty


@njit(cache=True)
def _field(ts, valid, is_valid, w, h, coarse, cw, ch):
    nnf = np.full(w*h, -1, np.int32)
    state = _seed(1 if cw == 0 else 2)
    for p in ts:
        if cw:
            x, y = p % w, p // w
            c = coarse[min(ch-1,y//2)*cw+min(cw-1,x//2)]
            if c >= 0:
                qx = min(w-1, (c % cw)*2+x%2)
                qy = min(h-1, (c // cw)*2+y%2)
                q = qy*w+qx
                if is_valid[q]:
                    nnf[p] = q
                    continue
        state, rnd = _next(state)
        nnf[p] = valid[int(rnd % np.uint64(len(valid)))]
    return nnf


@njit(cache=True)
def _distance(px, w, h, p, q, limit):
    x, y, qx, qy = p % w, p // w, q % w, q // w
    x0, x1 = max(-3,-x), min(3,w-1-x)
    y0, y1 = max(-3,-y), min(3,h-1-y)
    cap = limit * np.float32((x1-x0+1)*(y1-y0+1))
    d = np.float32(0)
    n = 0
    for dy in range(y0,y1+1):
        for dx in range(x0,x1+1):
            for c in range(4):
                e = px[(y+dy)*w+x+dx,c] - px[(qy+dy)*w+qx+dx,c]
                d += e*e
        n += x1-x0+1
        if d > cap:
            return np.float32(np.inf)
    return np.float32(d / max(n,1))


@njit(cache=True)
def _tryq(px,w,h,p,q,best,best_d,occ,is_valid):
    if q < 0 or q >= w*h or q == best or not is_valid[q]:
        return best,best_d
    pen = np.float32(20)*(occ[q]+np.float32(1))
    if pen < best_d:
        d = _distance(px,w,h,p,q,best_d-pen)+pen
        if d < best_d:
            return q,d
    return best,best_d


@njit(cache=True)
def _patch_match(px,w,h,nnf,ts,valid,is_valid,iterations):
    state = _seed(w*31+h)
    dist = np.full(w*h,np.float32(np.inf),np.float32)
    occ = np.zeros(w*h,np.float32)
    for p in ts:
        occ[nnf[p]] += np.float32(1)
    for p in ts:
        dist[p] = _distance(px,w,h,p,nnf[p],np.float32(np.inf))+np.float32(20)*occ[nnf[p]]
    for it in range(iterations):
        step = 1 if it % 2 == 0 else -1
        for k in range(len(ts)):
            p = ts[k if step == 1 else len(ts)-1-k]
            x,y = p%w,p//w
            was = nnf[p]
            best,best_d = int(was),dist[p]
            nx,ny = x-step,y-step
            if 0 <= nx < w and nnf[y*w+nx] >= 0:
                q = nnf[y*w+nx]
                if 0 <= q%w+step < w:
                    best,best_d = _tryq(px,w,h,p,q+step,best,best_d,occ,is_valid)
            if 0 <= ny < h and nnf[ny*w+x] >= 0:
                q = nnf[ny*w+x]
                if 0 <= q//w+step < h:
                    best,best_d = _tryq(px,w,h,p,q+step*w,best,best_d,occ,is_valid)
            radius = max(w,h)
            bx,by = best%w,best//w
            while radius >= 1:
                state,rnd = _next(state)
                rx = bx+int(rnd % np.uint64(2*radius+1))-radius
                state,rnd = _next(state)
                ry = by+int(rnd % np.uint64(2*radius+1))-radius
                if 0 <= rx < w and 0 <= ry < h:
                    best,best_d = _tryq(px,w,h,p,ry*w+rx,best,best_d,occ,is_valid)
                radius //= 2
            for _ in range(4):
                state,rnd = _next(state)
                q = valid[int(rnd % np.uint64(len(valid)))]
                best,best_d = _tryq(px,w,h,p,q,best,best_d,occ,is_valid)
            if best != was:
                occ[was] -= np.float32(1)
                occ[best] += np.float32(1)
            nnf[p],dist[p] = best,best_d


@njit(cache=True)
def _vote(px,hole,w,h,nnf,ts,sharp):
    acc = np.zeros_like(px)
    wsum = np.zeros(w*h,np.float32)
    best_w = np.full(w*h,np.float32(-1),np.float32)
    dists = np.empty(len(ts),np.float32)
    for k in range(len(ts)):
        p = ts[k]
        dists[k] = _distance(px,w,h,p,nnf[p],np.float32(np.inf))
    sigma2 = max(np.float32(1),np.sort(dists)[len(ts)*3//4])
    pick = px.copy()
    for k in range(len(ts)):
        p = ts[k]
        q = nnf[p]
        wt = np.float32(math.exp(-dists[k]/(2*sigma2)))
        x,y,qx,qy = p%w,p//w,q%w,q//w
        for dy in range(-3,4):
            ty = y+dy
            if ty < 0 or ty >= h:
                continue
            for dx in range(-3,4):
                tx = x+dx
                if tx < 0 or tx >= w:
                    continue
                t = ty*w+tx
                if not hole[t]:
                    continue
                s = (qy+dy)*w+qx+dx
                for c in range(4):
                    acc[t,c] += wt*px[s,c]
                wsum[t] += wt
                if sharp and wt > best_w[t]:
                    best_w[t] = wt
                    pick[t] = px[s]
    for t in range(w*h):
        if hole[t] and wsum[t] > 0:
            if sharp:
                px[t] = pick[t]
            else:
                px[t] = acc[t]/wsum[t]


def inpaint(image, hole, progress=None):
    """Return RGBA uint8 pixels. Raise FillError if no complete source patch exists."""
    rgba = np.asarray(image.convert("RGBA") if isinstance(image,Image.Image) else image, dtype=np.uint8)
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError("fill requires straight-alpha RGBA pixels")
    hole = np.asarray(hole,dtype=np.bool_)
    if hole.shape != rgba.shape[:2]:
        raise ValueError("hole dimensions differ from the picture")
    if not hole.any():
        return rgba.copy()
    levels = [(rgba.astype(np.float32),hole)]
    while max(levels[-1][1].shape) > 96 and min(levels[-1][1].shape) > 24:
        px, mask = _downsample(*levels[-1])
        if mask.all():
            break
        levels.append((px,mask))
    levels[-1] = (_diffuse(*levels[-1]),levels[-1][1])
    coarse_field = np.empty(0,np.int32)
    cw = ch = 0
    for li in range(len(levels)-1,-1,-1):
        px,mask = levels[li]
        h,w = mask.shape
        ts,valid,is_valid = _marks(mask,3)
        if not len(valid):
            raise FillError("No valid 7×7 source patch remains outside the extraction hole")
        nnf = _field(ts,valid,is_valid,w,h,coarse_field,cw,ch)
        if cw:
            _upsample(levels[li+1][0],px,mask)
            _vote(px.reshape(-1,4),mask.ravel(),w,h,nnf,ts,False)
        rounds = 8 if li == len(levels)-1 else 5
        for round_ in range(rounds):
            _patch_match(px.reshape(-1,4),w,h,nnf,ts,valid,is_valid,3 if round_ == 0 else 2)
            _vote(px.reshape(-1,4),mask.ravel(),w,h,nnf,ts,round_ >= rounds//2)
            if progress:
                progress((len(levels)-1-li+(round_+1)/rounds)/len(levels),"Filling background")
        coarse_field,cw,ch = nnf,w,h
    out = rgba.copy()
    # Swift rounds nonnegative channels to nearest, ties away from zero.
    out[hole] = np.clip(np.floor(levels[0][0][hole]+0.5),0,255).astype(np.uint8)
    return out


class ImageFill:
    inpaint = staticmethod(inpaint)


def run(job, progress):
    image = Image.open(job["image"]).convert("RGBA")
    mask = np.asarray(Image.open(job["hole"]).convert("L")) != 0
    out = inpaint(image,mask,progress)
    destination = Path(job["output"])
    temporary = destination.with_suffix(".tmp.png")
    try:
        Image.fromarray(out).save(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output":str(destination),"generation":job.get("generation")}


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(run)
