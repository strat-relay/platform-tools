"""Portable resolution for trading-platform runtime state.

The default deliberately remains the historical ``<checkout>/runtime`` path.
An explicit directory is only an address change; it does not create authority
or alter ownership generations.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent


def trading_platform_runtime_dir(*, root: Path = ROOT,
                                  environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("TRADING_PLATFORM_RUNTIME_DIR")
    if not configured:
        return root / "runtime"
    path = Path(configured).expanduser()
    return path if path.is_absolute() else root / path


def require_trading_platform_runtime(*, mode: str | None = None,
                                     root: Path = ROOT,
                                     environ: Mapping[str, str] | None = None) -> Path:
    """Resolve runtime state and fail closed for REAL mode when absent.

    This function never creates the directory and never writes ownership or
    authority state.  Callers that need a writable runtime may create child
    paths after this check, preserving the existing lifecycle semantics.
    """
    path = trading_platform_runtime_dir(root=root, environ=environ)
    if mode == "REAL_EXECUTION" and not path.is_dir():
        raise RuntimeError("TRADING_PLATFORM_RUNTIME_DIR_UNAVAILABLE")
    return path
