"""Spawn environment and import preflight for the Mac screen server (stdlib only, so it runs anywhere).

The gateway on a Mac is often an isolated base interpreter (``python -I -c <launcher>``) whose launcher adds
the venv's ``site-packages`` to ``sys.path`` in-process. A plain ``python -m tools.bot_desktop.macos.server``
child inherits none of that, so third-party imports (Pillow) fail with an opaque early exit. The fix hands
the child the parent's ``site-packages`` directories through ``PYTHONPATH`` (nothing else is forwarded) and
checks, in the child's own resolved environment, that the server's imports resolve before anything is spawned.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

# Third-party modules the macos package imports at module level. A test keeps this list honest.
REQUIRED_MODULES: tuple[str, ...] = ("PIL",)
_PACKAGE_NAMES = {"PIL": "Pillow"}
_SITE_DIR_NAMES = ("site-packages", "dist-packages")
_PROBE_TIMEOUT_S = 30.0


def site_packages_entries(paths: Optional[Iterable[str]] = None) -> list[str]:
    """The existing ``site-packages`` / ``dist-packages`` directories of ``paths`` (default ``sys.path``), in
    order and without duplicates. The stdlib and the repo root are deliberately left out."""
    seen: list[str] = []
    for raw in (sys.path if paths is None else paths):
        if not raw or Path(raw).name not in _SITE_DIR_NAMES or raw in seen or not os.path.isdir(raw):
            continue
        seen.append(raw)
    return seen


def with_parent_site_packages(env: dict[str, str], paths: Optional[Iterable[str]] = None) -> dict[str, str]:
    """``env`` plus ``PYTHONPATH`` = the parent's site-packages dirs (then any PYTHONPATH already in ``env``).
    Works for the isolated launcher and for a normal venv run, and keeps the interpreter unchanged."""
    out = dict(env)
    entries = site_packages_entries(paths)
    entries += [p for p in out.get("PYTHONPATH", "").split(os.pathsep) if p and p not in entries]
    if entries:
        out["PYTHONPATH"] = os.pathsep.join(entries)
    return out


def missing_in_child(env: dict[str, str], *, cwd: Optional[str] = None, python: Optional[str] = None,
                     modules: Iterable[str] = REQUIRED_MODULES) -> list[str]:
    """Modules that do not import in a child started with ``env`` (the exact env the server gets). A child
    that cannot even run the check reports every module as missing."""
    wanted = list(modules)
    argv = [python or sys.executable, "-c",
            "import importlib, json, sys\n"
            "bad = []\n"
            "for n in sys.argv[1:]:\n"
            "    try:\n"
            "        importlib.import_module(n)\n"
            "    except Exception:\n"
            "        bad.append(n)\n"
            "print(json.dumps(bad))\n", *wanted]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, env=env, cwd=cwd,
                              stdin=subprocess.DEVNULL)
        found = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return wanted
    return [n for n in found if n in wanted] if isinstance(found, list) else wanted


def missing_package_message(modules: Iterable[str]) -> str:
    names = ", ".join(_PACKAGE_NAMES.get(m, m) for m in modules)
    return f"A Python package the Mac screen needs is missing: {names}"


_SECRET_LIKE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_]*(key|token|secret|password)[A-Za-z0-9_]*\s*[=:]\s*\S+)")


def log_tail_summary(log_path: Path, *, max_chars: int = 200) -> Optional[str]:
    """The last non-empty line of ``launcher.log`` (for a traceback, its error line), trimmed and with anything
    secret-looking removed. None when there is no readable log."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    line = _SECRET_LIKE.sub("[hidden]", lines[-1])
    return line if len(line) <= max_chars else line[: max_chars - 3] + "..."

