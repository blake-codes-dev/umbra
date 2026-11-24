#!/usr/bin/env python3
"""
app_doctor.py — recovery for the DeepAI Code window itself (login, model, hung UI).

The app is Electron with a Chromium renderer. Windows UI Automation only sees the
web content when the renderer exposes an accessibility tree, so this tool has two
gears:

  * UIA gear  — element names are available: click by name, exact, verifiable.
  * blind gear — only the window exists: keyboard fallback (Tab/Enter) plus a
    screenshot, and the result is reported as "attempted", never as success.

Gears are chosen automatically per call. Launching the app with
--force-renderer-accessibility (supervisor does this) upgrades every later run to
the UIA gear.

    python tools/app_doctor.py --check
    python tools/app_doctor.py --login
    python tools/app_doctor.py --model "Opus"
    python tools/app_doctor.py --auto
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "state" / "app" / "doctor.json"
ACCOUNT = "jordanscom8"
TITLE = "DeepAI Code"

SIGN_IN = re.compile(r"sign\s*in|log\s*in|continue with google|signin", re.I)
SIGNED_IN = re.compile(r"new chat|new thread|ask anything|message deepai|search chats|"
                       r"what can i help", re.I)


def log(msg: str) -> None:
    print(msg)


def app_window():
    import uiautomation as auto

    for win in auto.GetRootControl().GetChildren():
        if win.ControlTypeName == "WindowControl" and (win.Name or "") == TITLE:
            return auto, win
    return auto, None


def texts(win, budget: float = 3.0) -> list[str]:
    """Named descendants, time-boxed. Empty list means accessibility is off."""
    found: list[str] = []
    started = time.monotonic()
    stack = [(win, 0)]
    seen = 0
    while stack and time.monotonic() - started < budget and seen < 4000:
        element, depth = stack.pop()
        try:
            children = element.GetChildren()
        except Exception:  # noqa: BLE001 - UIA throws on dead nodes
            continue
        for child in children:
            seen += 1
            name = (child.Name or "").strip()
            if name:
                found.append(name)
            if depth < 14:
                stack.append((child, depth + 1))
    return found


def classify(names: list[str]) -> str:
    if not names:
        return "unknown"
    if any(SIGN_IN.search(n) and not SIGNED_IN.search(n) for n in names):
        return "sign_in"
    if any(SIGNED_IN.search(n) for n in names):
        return "signed_in"
    return "unknown"


def click_named(auto, win, pattern: re.Pattern, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for element in descend(win, budget=1.5):
            if pattern.search(element.Name or ""):
                try:
                    element.Click(simulateMove=False)
                    return element.Name
                except Exception:  # noqa: BLE001
                    continue
        time.sleep(0.4)
    return None


def descend(win, budget: float = 2.0):
    started = time.monotonic()
    stack = [win]
    while stack and time.monotonic() - started < budget:
        element = stack.pop()
        try:
            children = element.GetChildren()
        except Exception:  # noqa: BLE001
            continue
        for child in children:
            yield child
            stack.append(child)


def focus(auto, win) -> None:
    try:
        win.SetActive()
        win.SetFocus()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)


def oauth_window(auto):
    for win in auto.GetRootControl().GetChildren():
        name = (win.Name or "")
        if win.ControlTypeName == "WindowControl" and re.search(r"google|sign in", name, re.I):
            return win
    return None


def login() -> dict:
    auto, win = app_window()
    if not win:
        return {"action": "login", "result": "no_window"}
    focus(auto, win)
    before = texts(win)
    state = classify(before)
    if state == "signed_in":
        return {"action": "login", "result": "already_signed_in"}

    clicked = click_named(auto, win, re.compile(r"google|sign\s*in|log\s*in|continue", re.I), 4)
    if not clicked:
        # blind gear: the sign-in button is usually the first focusable control
        auto.SendKeys("{TAB}")
        time.sleep(0.3)
        auto.SendKeys("{ENTER}")
        log("blind gear: sent Tab, Enter")

    time.sleep(3)
    popup = oauth_window(auto)
    if popup:
        pnames = texts(popup)
        target = next((n for n in pnames if ACCOUNT.lower() in n.lower()), None)
        if target and click_named(auto, popup, re.compile(re.escape(target), re.I), 3):
            log(f"picked account {target!r}")
        else:
            try:
                popup.SetActive()
                auto.SendKeys("{ENTER}")
                log("account chooser: sent Enter")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(4)

    after = texts(win)
    final = classify(after)
    return {"action": "login", "clicked": clicked, "before": state, "after": final,
            "popup": bool(popup), "result": "signed_in" if final == "signed_in" else "attempted"}


def select_model(name: str) -> dict:
    auto, win = app_window()
    if not win:
        return {"action": "model", "result": "no_window"}
    focus(auto, win)
    opened = click_named(auto, win, re.compile(r"model|" + re.escape(name), re.I), 3)
    if not opened:
        return {"action": "model", "result": "picker_not_found"}
    time.sleep(0.8)
    chosen = click_named(auto, win, re.compile(re.escape(name), re.I), 4)
    return {"action": "model", "requested": name, "opened": opened, "chosen": chosen,
            "result": "selected" if chosen else "option_not_found"}


def check() -> dict:
    auto, win = app_window()
    if win is None:
        return {"window": False, "state": "no_window"}
    names = texts(win)
    return {"window": True, "state": classify(names), "named_elements": len(names),
            "sample": names[:8],
            "accessibility": "on" if names else "off",
            "rect": [win.BoundingRectangle.left, win.BoundingRectangle.top,
                     win.BoundingRectangle.right, win.BoundingRectangle.bottom]}


def save(result: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = {"at": datetime.now().isoformat(timespec="seconds"), **result}
    OUT.write_text(json.dumps(result, indent=1), encoding="utf-8")
    log(json.dumps(result, ensure_ascii=False))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--login", action="store_true")
    group.add_argument("--model")
    group.add_argument("--auto", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        save(check())
    elif args.login:
        save(login())
    elif args.model:
        save(select_model(args.model))
    else:
        state = check()
        if state["state"] == "sign_in":
            result = login()
        elif state["state"] == "unknown" and state.get("accessibility") == "off":
            result = {"action": "auto", "result": "blind",
                      "note": "relaunch with --force-renderer-accessibility to enable UIA"}
        else:
            result = {"action": "auto", "result": "healthy", "state": state["state"]}
        save(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
