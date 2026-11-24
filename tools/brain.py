"""Two local brains behind one call.

qwen3:8b  - ollama, always resident, ~40 tok/s, follows short instructions well.
inkling2  - llama.cpp b12600, ~8.8 tok/s on the 2060, better reasoning, needs 11.2 GB VRAM.

They cannot both sit in 12 GB, so the Inkling server is started on demand and
stopped after `IDLE_STOP` seconds without a request. qwen is never stopped.

  python tools/brain.py "question"                  # qwen
  python tools/brain.py --model inkling "question"  # inkling
  python tools/brain.py --stop                      # free the VRAM
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
PID_FILE = STATE / "llama.pid"
LOG_FILE = STATE / "llama.log"
OLLAMA = "http://127.0.0.1:11434"
LLAMA = "http://127.0.0.1:8081"
IDLE_STOP = 600


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


def qwen(messages: list[dict], max_tokens: int = 512, temperature: float = 0.2) -> str:
    body = {
        "model": "qwen3:8b",
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    return _post(OLLAMA + "/api/chat", body, timeout=300)["message"]["content"]


def llama_up() -> bool:
    try:
        return _get(LLAMA + "/health") == 200
    except Exception:
        return False


def start_llama(wait: float = 300) -> bool:
    if llama_up():
        return True
    exe = ROOT / "tools" / "llama" / "llama-server.exe"
    model = ROOT / "models" / "inkling-2-mini-iq2.gguf"
    if not exe.exists() or not model.exists():
        return False
    log = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(exe), "-m", str(model), "-ngl", "99", "-c", "4096", "--port", "8081",
         "--host", "127.0.0.1", "--no-webui"],
        stdout=log, stderr=subprocess.STDOUT, cwd=str(exe.parent),
        creationflags=0x00000008,  # DETACHED_PROCESS
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if llama_up():
            return True
        if proc.poll() is not None:
            return False
        time.sleep(2)
    return llama_up()


def stop_llama() -> bool:
    pid = None
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    if not pid:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    for _ in range(30):
        if not llama_up():
            return True
        time.sleep(1)
    return not llama_up()


def inkling(messages: list[dict], max_tokens: int = 512, temperature: float = 0.2) -> str:
    if not start_llama():
        raise RuntimeError("inkling server did not start (see state/llama.log)")
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    data = _post(LLAMA + "/v1/chat/completions", body, timeout=900)
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("reasoning_content") or "").strip()
    return text


def ask(prompt: str, model: str = "qwen", system: str | None = None,
        max_tokens: int = 512) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    return inkling(messages, max_tokens) if model == "inkling" else qwen(messages, max_tokens)


def main() -> int:
    args = sys.argv[1:]
    if "--stop" in args:
        print("stopped" if stop_llama() else "nothing running")
        return 0
    model = "inkling" if "--model" in args and args[args.index("--model") + 1] == "inkling" else "qwen"
    prompt = " ".join(a for a in args if a not in ("--model", "qwen", "inkling"))
    if not prompt.strip():
        print(__doc__)
        return 2
    t0 = time.monotonic()
    print(ask(prompt, model=model))
    print(f"\n[{model} {time.monotonic() - t0:.1f}s]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
