"""Make NVIDIA's CUDA libraries visible to CTranslate2 on Windows.

faster-whisper runs on CTranslate2, which loads cuBLAS 12 and cuDNN 9 at runtime
but doesn't ship them. The CUDA 12 torch wheel does, and importing torch loads
every DLL in its lib folder into the process, so CTranslate2 finds them by name.
Call ``prepare()`` before importing faster_whisper.
"""

from __future__ import annotations

import os
import sys


def driver() -> bool:
    """Whether NVIDIA's driver is there (its nvcuda.dll): without it there is no CUDA GPU, and
    nothing need be loaded to find that out."""
    if sys.platform != "win32":
        return True
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.exists(os.path.join(windows, "System32", "nvcuda.dll"))


def prepare() -> bool:
    """Load the CUDA DLLs. Returns True when a CUDA GPU is usable."""
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())
