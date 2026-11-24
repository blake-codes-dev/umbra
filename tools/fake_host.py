#!/usr/bin/env python3
"""Fake injected host: serves \\\\.\\pipe\\umbra-<pid> and speaks the panel protocol.

Lets the GUI be driven end to end with no Roblox client and no DLL.

  python tools/fake_host.py --pid 1234
  python tools/fake_host.py --pid 1234 --script "print(1)"   # push a line then exit

Protocol: newline-delimited JSON, see src/app/Umbra/Services/PipeBackend.cs.
"""
from __future__ import annotations

import argparse
import json
import threading
import time

import win32file
import win32pipe


def send(handle, payload: dict) -> None:
    win32file.WriteFile(handle, (json.dumps(payload) + "\n").encode())


def serve(pid: int, script: str | None) -> None:
    name = rf"\\.\pipe\umbra-{pid}"
    handle = win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 0, None)
    print("waiting on", name, flush=True)
    win32pipe.ConnectNamedPipe(handle, None)
    print("panel connected", flush=True)
    running = True

    def reader() -> None:
        nonlocal running
        buffer = b""
        while running:
            try:
                _, chunk = win32file.ReadFile(handle, 65536)
            except Exception:
                running = False
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                op = message.get("op")
                print("<-", message.get("op"), message.get("id", ""), flush=True)
                if op == "hello":
                    send(handle, {"op": "state", "state": "attached", "build": "0.1.0-fake",
                                  "modules": 0, "threads": 0, "windows": 0, "kb": 8})
                elif op == "exec":
                    script_id = message.get("id")
                    send(handle, {"op": "log", "kind": "print", "script": script_id,
                                  "text": "fake host: running " + message.get("code", "")[:40]})
                    time.sleep(0.15)
                    if "boom" in message.get("code", ""):
                        send(handle, {"op": "fail", "id": script_id,
                                      "text": "fake_host: script raised"})
                    else:
                        send(handle, {"op": "done", "id": script_id, "ms": 150})
                elif op == "bye":
                    running = False
                    return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    if script:
        time.sleep(1.0)
        send(handle, {"op": "log", "kind": "warn", "text": "pushed: " + script})
    while running:
        time.sleep(0.2)
    print("closing", flush=True)
    win32file.CloseHandle(handle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=4242)
    ap.add_argument("--script", default=None)
    args = ap.parse_args()
    serve(args.pid, args.script)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
