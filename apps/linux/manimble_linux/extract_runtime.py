"""The optional IS-Net extraction model: explicitly consented download and integrity checks.

The model runs with onnxruntime in Manimble's own private Python; only the weights are optional. This module stays
Qt-free at import time because its ``__main__`` is the download worker. Downloading is never a launch side effect:
the ExtractEditor starts it only after its Download action is accepted.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

MODEL = {
    "name":"isnet-general-use",
    "url":"https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
    "bytes":178648008,
    "sha256":"60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
    "license":"Apache-2.0",
}
MODEL_FILE = "isnet-general-use.onnx"
_PARTIAL = ".isnet-download-"
DISCLOSURE = (
    "Download the IS-Net subject-finding model (179 MB, Apache-2.0)? "
    "It runs locally on your CPU in a few seconds per picture; pictures are not uploaded. "
    "Elements may differ from Apple Vision: subjects that touch come out as one element. "
    "Whole Box, Keep/Remove and background fill work without the model."
)


class ExtractionRuntimeError(RuntimeError):
    pass


def verify_model(path, manifest=None):
    """Verify before promotion AND before loading. No trust in filenames."""
    manifest = MODEL if manifest is None else manifest
    path = Path(path)
    if path.stat().st_size != manifest["bytes"]:
        raise ExtractionRuntimeError(f"Model size mismatch: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        raise ExtractionRuntimeError(f"Model SHA-256 mismatch: {path}; refusing to load unverified model weights")
    return path


def download_model(destination, progress, manifest=None):
    manifest = MODEL if manifest is None else manifest
    destination = Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        return verify_model(destination,manifest)
    # A cancelled download is killed mid-write; its partial file is removed by the next attempt.
    for stale in destination.parent.glob(_PARTIAL+"*"):
        stale.unlink(missing_ok=True)
    fd,name = tempfile.mkstemp(prefix=_PARTIAL,dir=destination.parent)
    temporary = Path(name)
    try:
        count = 0
        with os.fdopen(fd,"wb") as out, urllib.request.urlopen(manifest["url"],timeout=60) as response:
            while chunk := response.read(1024*1024):
                count += len(chunk)
                if count > manifest["bytes"]:
                    raise ExtractionRuntimeError("Model download exceeds its pinned size")
                out.write(chunk)
                progress(count/manifest["bytes"],"Downloading the IS-Net model")
            out.flush()
            os.fsync(out.fileno())
        verify_model(temporary,manifest)
        os.replace(temporary,destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def provision(job, progress):
    """Worker entry. The only network operation, covered by one explicit action."""
    if not job.get("consented"):
        raise ExtractionRuntimeError("Downloading the model requires explicit consent")
    return {"model":str(download_model(job["model"],progress))}


class ExtractRuntime:
    def __init__(self, paths, runtime):
        self.paths,self.runtime = paths,runtime
        self.model = paths.models / MODEL_FILE

    @property
    def available(self):
        # Digest checking happens in the inference worker immediately before load.
        return self.model.is_file()

    def install_worker(self):
        from .workers import WorkerProcess
        return WorkerProcess(self.runtime.python,"extract_runtime",{"consented":True,"model":str(self.model)},
                             env=self.runtime.child_env())

    def inference_worker(self, image, directory, box=None):
        if not self.available:
            raise ExtractionRuntimeError("Download the extraction model first; Whole Box and brushes remain available")
        from .workers import WorkerProcess
        return WorkerProcess(self.runtime.python,"extract_worker",{
            "image":str(image),"directory":str(directory),"model":str(self.model),"box":box,
        },env=self.runtime.child_env())


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(provision)
