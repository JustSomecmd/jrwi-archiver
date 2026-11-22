"""Giving memory back after big jobs.

Reading pages builds large trees of objects that refer to each other, which Python only frees when its cycle
collector runs, and memory freed inside the process isn't handed back to the system by itself. After a check, a
re-read or a line-up the service would otherwise keep its peak size until it restarts.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging

log = logging.getLogger(__name__)

_trim = None
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _trim = _libc.malloc_trim
    _trim.argtypes = [ctypes.c_size_t]
    _trim.restype = ctypes.c_int
except (OSError, AttributeError):  # not glibc (macOS, musl): collecting is all there is
    _trim = None


def release() -> None:
    """Free what's no longer used and hand it back to the system."""
    gc.collect()
    if _trim is not None:
        try:
            _trim(0)
        except Exception:  # never let tidying up break a job
            log.debug("malloc_trim failed", exc_info=True)

