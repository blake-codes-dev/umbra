#!/usr/bin/env python3
"""guard.py - the only code allowed to delete, and the definition of "mine".

Local models never see the filesystem: they choose one action from a fixed
whitelist, so they cannot name a path, invent a command, or delete anything.
This module is the second fence for the Python acting on their behalf.

    python tools/guard.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()

ALLOWED = [ROOT, HOME / "AppData" / "Local" / "Roblox", Path(r"C:\Program Files\WindowsApps")]
PROTECTED = [HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Pictures",
             HOME / "Videos", HOME / "Music", HOME / "OneDrive", HOME / ".ssh"]


def resolve(path) -> Path:
    return Path(path).expanduser().resolve()


def under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_protected(path) -> bool:
    target = resolve(path)
    return any(under(target, resolve(p)) for p in PROTECTED)


def is_allowed(path) -> bool:
    target = resolve(path)
    return any(under(target, resolve(a)) for a in ALLOWED)


def may_delete(path) -> bool:
    target = resolve(path)
    return (not is_protected(target)) and is_allowed(target) and target != resolve(ROOT)


def safe_delete(path) -> bool:
    target = resolve(path)
    if not target.exists() or not may_delete(target):
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        try:
            target.unlink()
        except OSError:
            return False
    return True


def audit() -> dict:
    return {
        "root_deletable": may_delete(ROOT),
        "state_deletable": may_delete(ROOT / "state"),
        "desktop_deletable": may_delete(HOME / "Desktop"),
        "documents_deletable": may_delete(HOME / "Documents"),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    report = audit()
    for key, value in report.items():
        print(f"{key}: {value}")
    ok = (not report["root_deletable"] and not report["desktop_deletable"]
          and not report["documents_deletable"] and report["state_deletable"])
    print("policy ok" if ok else "POLICY BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
