"""Explicitly consented optional SAM runtime provisioning and checkpoint integrity.

This module stays Qt-free in worker interpreters. Installation is never a launch
side effect: the ExtractEditor starts it only after its Install action is accepted.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import urllib.request

CHECKPOINT = {
    "url":"https://huggingface.co/facebook/sam2.1-hiera-tiny/resolve/de431c4043854a71d8101e17995dfe596bf101a5/sam2.1_hiera_tiny.pt",
    "bytes":156008466,
    "sha256":"7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69",
    "config":"configs/sam2.1/sam2.1_hiera_t.yaml",
    "license":"Apache-2.0",
}
CHECKPOINT_NAME = "sam2.1_hiera_tiny.pt"
DISCLOSURE = (
    "Install SAM 2.1 Hiera Tiny (Apache-2.0) in a separate private Python environment? "
    "This downloads CPU PyTorch and its dependencies plus a 156 MB checkpoint (the environment needs additional disk space). "
    "Inference runs locally on your CPU in float32 and can be slow; images are not uploaded. "
    "Segments may differ from Apple Vision. Manual Box/Whole Box/Keep/Remove and background fill need no model."
)
_temporary_download = None


class ExtractionRuntimeError(RuntimeError):
    pass


def supported_python(version):
    version = tuple(version[:3])
    return version[:2] >= (3,11) and version[:2] <= (3,14) and version != (3,14,1)


def verify_checkpoint(path, manifest=None):
    """Verify before promotion AND before model deserialization. No trust in filenames."""
    manifest = CHECKPOINT if manifest is None else manifest
    path = Path(path)
    if path.stat().st_size != manifest["bytes"]:
        raise ExtractionRuntimeError(f"Checkpoint size mismatch: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest["sha256"]:
        raise ExtractionRuntimeError(f"Checkpoint SHA-256 mismatch: {path}; refusing to load unverified model weights")
    return path


def download_checkpoint(destination, progress, manifest=None):
    global _temporary_download
    manifest = CHECKPOINT if manifest is None else manifest
    destination = Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        return verify_checkpoint(destination,manifest)
    fd,name = tempfile.mkstemp(prefix=".sam-checkpoint-",dir=destination.parent)
    temporary = Path(name)
    _temporary_download = temporary
    try:
        count = 0
        with os.fdopen(fd,"wb") as out, urllib.request.urlopen(manifest["url"],timeout=60) as response:
            while chunk := response.read(1024*1024):
                count += len(chunk)
                if count > manifest["bytes"]:
                    raise ExtractionRuntimeError("Checkpoint download exceeds its pinned size")
                out.write(chunk)
                progress(count/manifest["bytes"],"Downloading SAM 2.1 checkpoint")
            out.flush()
            os.fsync(out.fileno())
        verify_checkpoint(temporary,manifest)
        os.replace(temporary,destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        _temporary_download = None


def _checked(command, env):
    # This executes only in the single-threaded provisioning worker. Logs go to
    # stderr, never onto the JSON protocol stream; no shell interpolation.
    subprocess.run(command,env=env,check=True,stdout=sys.stderr,stderr=sys.stderr)


def provision(job, progress):
    """Worker entry. Every network/install operation is covered by one explicit action."""
    if not job.get("consented"):
        raise ExtractionRuntimeError("Model installation requires explicit Install extraction model consent")
    # Put the installer and pip/build descendants in their own group. The GUI
    # cancels with SIGTERM; the handler kills the complete group, not just pip's parent.
    os.setsid()
    def cancel(*_):
        if _temporary_download is not None:
            _temporary_download.unlink(missing_ok=True)
        os.killpg(os.getpgrp(),signal.SIGKILL)
    signal.signal(signal.SIGTERM,cancel)
    python = str(job["python"])
    env = dict(os.environ)
    for name in ("PYTHONHOME","PYTHONPATH","PIP_INDEX_URL","PIP_EXTRA_INDEX_URL"):
        env.pop(name,None)
    version = subprocess.check_output([python,"-I","-c","import sys,json; print(json.dumps(list(sys.version_info[:3])))"],env=env,text=True)
    import json
    version = json.loads(version)
    if not supported_python(version):
        raise ExtractionRuntimeError("Extraction needs Python 3.11–3.14 except 3.14.1; choose another installed interpreter. Manual tools remain available.")
    target = Path(job["target"])
    target.parent.mkdir(parents=True,exist_ok=True)
    Path(job["ready"]).unlink(missing_ok=True)
    progress(0.02,"Creating optional extraction environment")
    _checked([python,"-I","-m","venv",str(target.parent.parent)],env)
    # Use --isolated to ignore user pip config; only torch uses the CPU index.
    pip = [str(target),"-I","-m","pip","--isolated"]
    progress(0.1,"Installing build tools from PyPI")
    _checked(pip+["install","--index-url","https://pypi.org/simple","setuptools>=61.0","wheel"],env)
    progress(0.2,"Installing CPU PyTorch")
    _checked(pip+["install","--index-url","https://download.pytorch.org/whl/cpu","torch==2.15.0+cpu","torchvision==0.29.2+cpu"],env)
    progress(0.5,"Installing pinned SAM 2.1 source")
    env["SAM2_BUILD_CUDA"] = "0"
    _checked(pip+["install","--index-url","https://pypi.org/simple","--no-build-isolation","-r",str(job["requirements"])],env)
    checkpoint = download_checkpoint(job["checkpoint"],lambda x,t:progress(0.7+0.3*x,t))
    # Import validation stays in the worker, not the GUI.
    _checked([str(target),"-I","-c","import torch, numpy, PIL; from sam2.build_sam import build_sam2"],env)
    ready = Path(job["ready"])
    temporary = ready.with_suffix(".tmp")
    temporary.write_text("sam2.1-hiera-tiny\n",encoding="utf-8")
    temporary.replace(ready)
    return {"python":str(target),"checkpoint":str(checkpoint)}


class ExtractRuntime:
    def __init__(self, paths, runtime):
        self.paths,self.runtime = paths,runtime
        self.python = paths.extract_python
        self.checkpoint = paths.models / CHECKPOINT_NAME
        self.ready_file = self.python.parent.parent / ".manimble-ready"

    @property
    def available(self):
        # Digest checking happens in the inference worker immediately before load.
        return self.python.is_file() and self.ready_file.is_file() and self.checkpoint.is_file()

    def install_worker(self, python=None):
        from .workers import WorkerProcess, WorkerCancelled
        from PySide6.QtCore import QTimer
        class Installer(WorkerProcess):
            def cancel(self):
                self._cancelled = True
                if self.proc is not None:
                    self.proc.terminate()
                    QTimer.singleShot(3000,self.proc.kill)
                if not self.future.done():
                    self.future.set_exception(WorkerCancelled("Model installation cancelled; manual tools remain available"))
        return Installer(self.runtime.python,"extract_runtime",{
            "consented":True,"python":str(python or self.runtime.python),"target":str(self.python),
            "checkpoint":str(self.checkpoint),"ready":str(self.ready_file),
            "requirements":str(Path(__file__).resolve().parent.parent/"requirements-extract.txt"),
        },env=self.runtime.child_env())

    def inference_worker(self, image, directory, box=None):
        if not self.available:
            raise ExtractionRuntimeError("Install extraction model first; manual Whole Box and brushes remain available")
        from .workers import WorkerProcess
        return WorkerProcess(str(self.python),"extract_worker",{
            "image":str(image),"directory":str(directory),"checkpoint":str(self.checkpoint),"box":box,
        },env=self.runtime.child_env())


if __name__ == "__main__":
    from .worker_protocol import worker_main
    worker_main(provision)
