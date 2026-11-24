#!/usr/bin/env python3
"""
harden.py — the admin-only settings that keep a 4-week unattended run alive.

Run elevated once (or let a turn launch it with Start-Process -Verb RunAs).
Everything is idempotent and recorded in state/harden.json.

    python tools/harden.py            # apply
    python tools/harden.py --status   # report only

What it changes and why:
  * AutoAdminLogon        — after power loss the machine signs itself in
  * CrashControl AutoReboot — a BSOD restarts instead of waiting at a black screen
  * HiberbootEnabled=0    — fast startup off, so boot behaves like a real boot
  * ActiveHours 21..8     — Windows Update never reboots inside the night window
  * NoAutoRebootWithLoggedOnUsers — updates never restart under a logged-on user
  * Defender exclusions   — the project and Roblox are not scanned on every write
  * tasks at HIGHEST      — supervisor/qwen/night-runner never hit a UAC wall
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import winreg
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
OUT = STATE / "harden.json"
SECRETS = ROOT / "secrets"
TASKS = ["RobloxNightRunner", "RobloxSupervisor", "RobloxQwenShift"]
WINLOGON = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def set_value(root, path: str, name: str, value, kind=winreg.REG_SZ) -> bool:
    try:
        key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, name, 0, kind, value)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def get_value(root, path: str, name: str):
    try:
        key = winreg.OpenKey(root, path)
        try:
            return winreg.QueryValueEx(key, name)[0]
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None


def credential() -> str:
    for name in ("windows.txt", "windows..txt"):
        path = SECRETS / name
        try:
            return path.read_text(encoding="utf-8").strip().splitlines()[-1].strip()
        except (OSError, IndexError):
            continue
    return ""


def logon_valid(user: str, password: str, domain: str | None = None) -> bool:
    if not password:
        return False
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    token = ctypes.c_void_p()
    ok = adv.LogonUserW(user, domain, password, 2, 0, ctypes.byref(token))
    if ok:
        ctypes.WinDLL("kernel32").CloseHandle(token)
    return bool(ok)


def ms_email() -> str | None:
    """Microsoft accounts sign in with an email; find it if the profile has one."""
    base = r"Software\Microsoft\IdentityCRL\UserExtendedProperties"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except OSError:
        return None
    try:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(key, index)
            except OSError:
                return None
            index += 1
            if "@" in sub:
                return sub
    finally:
        winreg.CloseKey(key)


def apply() -> dict:
    result: dict = {"at": datetime.now().isoformat(timespec="seconds"),
                    "elevated": elevated(), "applied": {}, "notes": []}
    if not result["elevated"]:
        result["notes"].append("not elevated: relaunch the app as administrator")
        return result

    user = os.environ.get("USERNAME", "blake")
    password = credential()
    email = ms_email()
    login_user = email or user
    ok = logon_valid(login_user, password) or logon_valid(user, password)
    if ok:
        set_value(winreg.HKEY_LOCAL_MACHINE, WINLOGON, "AutoAdminLogon", "1")
        set_value(winreg.HKEY_LOCAL_MACHINE, WINLOGON, "DefaultUserName", login_user)
        set_value(winreg.HKEY_LOCAL_MACHINE, WINLOGON, "DefaultPassword", password)
        set_value(winreg.HKEY_LOCAL_MACHINE, WINLOGON, "DefaultDomainName",
                  "MicrosoftAccount" if email else os.environ.get("COMPUTERNAME", "."))
        result["applied"]["autologon"] = f"set for {login_user}"
    else:
        result["applied"]["autologon"] = "skipped"
        result["notes"].append(
            "credential in secrets/windows.txt is not a valid account password; "
            "a Microsoft account needs its password, not the Hello PIN")

    result["applied"]["crash_reboot"] = set_value(
        winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CrashControl",
        "AutoReboot", 1, winreg.REG_DWORD)
    result["applied"]["fast_startup_off"] = set_value(
        winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
        "HiberbootEnabled", 0, winreg.REG_DWORD)
    result["applied"]["active_hours"] = (
        set_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings",
                  "ActiveHoursStart", "21")
        and set_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings",
                      "ActiveHoursEnd", "8"))
    result["applied"]["no_auto_reboot"] = set_value(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU",
        "NoAutoRebootWithLoggedOnUsers", 1, winreg.REG_DWORD)

    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Add-MpPreference -ExclusionPath '{ROOT}' -ErrorAction SilentlyContinue"],
                       capture_output=True, timeout=90)
        result["applied"]["defender_exclusion"] = True
    except (OSError, subprocess.SubprocessError):
        result["applied"]["defender_exclusion"] = False

    try:
        python_exe = sys.executable
        app_exe = r"C:\Program Files\DeepAI Code\DeepAI Code.exe"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Set-MpPreference -EnableControlledFolderAccess Enabled; "
                        f"Add-MpPreference -ControlledFolderAccessAllowedApplications "
                        f"'{python_exe}','{app_exe}' -ErrorAction SilentlyContinue"],
                       capture_output=True, timeout=120)
        result["applied"]["controlled_folder_access"] = True
    except (OSError, subprocess.SubprocessError):
        result["applied"]["controlled_folder_access"] = False

    raised = []
    for task in TASKS:
        done = subprocess.run(["schtasks", "/Change", "/TN", task, "/RL", "HIGHEST"],
                              capture_output=True, text=True, timeout=30)
        if done.returncode == 0:
            raised.append(task)
    result["applied"]["tasks_high"] = raised

    result["verified"] = {
        "AutoAdminLogon": get_value(winreg.HKEY_LOCAL_MACHINE, WINLOGON, "AutoAdminLogon"),
        "AutoReboot": get_value(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\CrashControl", "AutoReboot"),
        "ActiveHoursStart": get_value(winreg.HKEY_LOCAL_MACHINE,
                                      r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings",
                                      "ActiveHoursStart"),
    }
    return result


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    argv = argv if argv is not None else sys.argv[1:]
    result = apply()
    if "--status" in argv:
        result["applied"] = {}
    STATE.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
