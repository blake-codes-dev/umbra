#!/usr/bin/env python3
"""
vault.py — fingerprint the user's files, back them up, and prove nothing moved.

Three jobs:
  snapshot  record size+mtime for every file under the protected folders
  verify    diff today against the last snapshot; any change is listed by path
  backup    mirror the protected folders and the project to D:\\UmbraVault\\<stamp>

The local models never call this: they choose an action name, the action calls
vault. So a model cannot name a path, and the only code that writes outside the
project is this file's backup (copy out, never move, never delete).

    python tools/vault.py --auto       # snapshot/backup only when due
    python tools/vault.py --snapshot
    python tools/vault.py --verify
    python tools/vault.py --backup
    python tools/vault.py --restore "Documents/taxes.pdf"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "vault"
HOME = Path.home()
VAULT = Path("D:/UmbraVault")

PROTECTED = [
    HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Pictures",
    HOME / "Videos", HOME / "Music", HOME / "OneDrive",
]
PROJECT_PARTS = ["tools", "state", "baselines", "PLAN.md", "PRODUCT.md", "RUNBOOK.md",
                 "MORNING.md", "secrets"]
SKIP_NAMES = {"dump", "models", "__pycache__", ".git", "node_modules"}

SNAPSHOT_MAX_AGE = 20 * 3600
BACKUP_MAX_AGE = 7 * 24 * 3600
MAX_ENTRIES = 300_000


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def walk(root: Path, cap: int = MAX_ENTRIES):
    """Yield (relative path, size, mtime) without following reparse points."""
    if not root.exists():
        return
    count = 0
    stack = [root]
    while stack and count < cap:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in SKIP_NAMES:
                        stack.append(Path(entry.path))
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            yield str(Path(entry.path).relative_to(root)), int(info.st_size), int(info.st_mtime)
            count += 1


def snapshot(quiet: bool = False) -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[int]] = {}
    totals: dict[str, dict] = {}
    for folder in PROTECTED:
        if not folder.exists():
            continue
        size = 0
        count = 0
        for rel, length, mtime in walk(folder):
            key = f"{folder.name}/{rel}"
            files[key] = [length, mtime]
            size += length
            count += 1
        totals[folder.name] = {"files": count, "bytes": size}
    payload = {"at": datetime.now().isoformat(timespec="seconds"), "totals": totals,
               "files": files}
    latest = STATE / "manifest.json"
    latest.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    (STATE / f"manifest-{stamp()}.json").write_text(
        json.dumps({"at": payload["at"], "totals": totals}, indent=1), encoding="utf-8")
    if not quiet:
        for name, info in totals.items():
            print(f"  {name:<12} {info['files']:>7} files  {human(info['bytes'])}")
        print(f"snapshot: {sum(t['files'] for t in totals.values())} files")
    return payload


def load_latest() -> dict | None:
    try:
        return json.loads((STATE / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify(quiet: bool = False) -> dict:
    previous = load_latest()
    if not previous:
        return {"status": "no snapshot"}
    current = snapshot(quiet=True)
    before = previous.get("files", {})
    after = current["files"]
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    changed = [k for k in after if k in before and after[k] != before[k]]
    report = {"at": datetime.now().isoformat(timespec="seconds"), "added": len(added),
              "removed": len(removed), "changed": len(changed),
              "sample_added": added[:15], "sample_removed": removed[:15],
              "sample_changed": changed[:15]}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "drift.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    if not quiet:
        print(f"drift: +{len(added)} -{len(removed)} ~{len(changed)}")
        for key in (added + removed + changed)[:10]:
            print("   ", key)
    return report


def folder_size(path: Path) -> int:
    return sum(length for _, length, _ in walk(path))


def backup(quiet: bool = False) -> dict:
    VAULT.mkdir(parents=True, exist_ok=True)
    target = VAULT / stamp()
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(VAULT).free
    copied: dict[str, str] = {}
    for folder in PROTECTED:
        if not folder.exists():
            continue
        size = folder_size(folder)
        if size > free * 0.6:
            copied[folder.name] = f"skipped ({human(size)} > 60% of free)"
            continue
        dest = target / folder.name
        result = subprocess.run(["robocopy", str(folder), str(dest), "/E", "/R:1", "/W:1",
                                 "/NFL", "/NDL", "/NJH", "/NJS", "/XJ"],
                                capture_output=True)
        copied[folder.name] = "ok" if result.returncode < 8 else f"rc {result.returncode}"
        if not quiet:
            print(f"  {folder.name:<12} {human(size):>10}  {copied[folder.name]}")

    project = target / "project"
    project.mkdir(parents=True, exist_ok=True)
    for part in PROJECT_PARTS:
        source = ROOT / part
        if not source.exists():
            continue
        try:
            if source.is_dir():
                shutil.copytree(source, project / part, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*SKIP_NAMES))
            else:
                shutil.copy2(source, project / part)
        except OSError as error:
            copied[part] = f"error {error}"
    copied["project"] = "ok"

    manifest = {"at": datetime.now().isoformat(timespec="seconds"), "target": str(target),
                "parts": copied}
    (STATE / "backup.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if not quiet:
        print(f"backup: {target}")
    return manifest


def restore(relative: str) -> dict:
    try:
        latest = json.loads((STATE / "backup.json").read_text(encoding="utf-8"))["target"]
    except (OSError, json.JSONDecodeError, KeyError):
        return {"status": "no backup"}
    source = Path(latest) / relative
    if not source.exists():
        return {"status": f"not in backup: {relative}"}
    destination = HOME / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)
    return {"status": "restored", "from": str(source), "to": str(destination)}


def age_of(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 10 ** 9


def auto(quiet: bool = True) -> dict:
    """Verify first (against the previous manifest), then snapshot, then back up."""
    actions = {}
    due = age_of(STATE / "manifest.json") > SNAPSHOT_MAX_AGE
    if due and (STATE / "manifest.json").exists():
        actions["drift"] = verify(quiet=True)
    if due:
        snapshot(quiet=quiet)
        actions["snapshot"] = "done"
    if age_of(STATE / "backup.json") > BACKUP_MAX_AGE:
        backup(quiet=quiet)
        actions["backup"] = "done"
    return actions


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = argv if argv is not None else sys.argv[1:]
    if "--snapshot" in argv:
        snapshot()
    elif "--verify" in argv:
        verify()
    elif "--backup" in argv:
        backup()
    elif "--restore" in argv:
        index = argv.index("--restore")
        print(json.dumps(restore(argv[index + 1]), indent=1))
    else:
        print(json.dumps(auto(quiet="--quiet" in argv), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
