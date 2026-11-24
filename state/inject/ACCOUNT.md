# Injection test account

status: alt provided 2026-11-24 10:5x (user). App was signed out; user logged in via browser.
rule: inject on the alt only. The main account shares this IP and hardware, so a
      detection on the alt can still mark the machine — keep the footprint claims honest.

## Ban-safety rules for any live test
1. Never patch `.text`; only private memory + a borrowed thread.
2. Verify after each run: no new module-list entry, no new thread, no new window.
3. Keep the stub < 64 KB (it is 24 KB, one RX page + 61-byte table).
4. Prefer a real game with a loaded script so Lua pages are hot.
5. One change at a time; if the client dies, note the last action before restarting.
6. Leave the client alone (no reinjection loops) between milestones.

when a different account is logged in: update this file first.
