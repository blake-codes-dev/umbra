# RUNBOOK — 4 weeks unattended

The rule: **deterministic layers keep working without a brain; the brain is optional.**
If everything you see below fails, the machine still collects data and leaves it on disk.

## Who runs what

| Layer | Cadence | Job | Needs internet |
|---|---|---|---|
| `RobloxNightRunner` | every 5 min (runs the whole night) | jobs: observer 1 Hz, decoys, footprint, 10 Hz churn | no |
| `RobloxSupervisor` | every 5 min | health, mode switch, restarts, disk, tasks | no |
| `RobloxQwenShift` | every 15 min | offline dispatcher: one action from a whitelist | no (localhost) |
| Agent turns | 08:05, 12:05, 20:45 | reading state, real work, extending the plan | yes |
| App (DeepAI Code) | on demand | hosts the agent | yes |

Mode switch: supervisor writes `state/mode.json` on every online↔offline change and drops a
handoff file in `state/handoff/`. Qwen stands down the moment the internet returns.

## Failure matrix

| Failure | Detected by | Automatic response | Lost? |
|---|---|---|---|
| Worker crash | stale lock + 45 min heartbeat | task relaunch ≤5 min | one job |
| Worker hang | heartbeat age | supervisor kills pid, task relaunch | one job |
| Ollama down | `/api/tags` fails | `start_ollama` | one qwen cycle |
| Qwen returns junk | JSON schema check | deterministic rules | nothing |
| App closed/crashed | process check | `start_app` | agent turns until launch |
| Roblox closed at night | process check | `start_roblox` (protocol) | nothing |
| Disk < 8 GB | `shutil.disk_usage` | prune artifacts >3 days, compress logs | old artifacts |
| Scheduled task deleted | `schtasks /Query` | recreate | nothing |
| Internet down | socket probe | mode→qwen, qwen dispatcher | my thinking |
| Internet returns | socket probe | mode→agent + handoff file | nothing |
| Roblox update | version in footprints | data keeps flowing, digest flags | comparability |
| PC reboot | logon tasks + `startup/roblox-intel.cmd` | everything relaunches | night until logon |
| PC asleep | `powercfg` (must be off) | nothing | **everything** |
| Lock screen after reboot | — | interactive-only tasks do not run | **everything** |
| Provider outage at 08:05 | scheduled task fails | 12:05 retry, NEEDS_HUMAN after 26 h | one turn |

## The three things that can actually kill the run

1. **Sleep/hibernate.** Set AC standby and hibernate to 0. Verify with `powercfg /q` or by
   checking `state/night/runs.jsonl` has jobs at 03:00.
2. **Reboot to the lock screen while away.** Tasks are interactive-only (the account is not
   elevated, no stored password). Windows only reboots with consent while someone is logged on,
   but a power cut does not ask. Options: enable auto-login, or accept losing the rest of the trip.
3. **App or Ollama removed/updated to a different path.** Startup cmd and supervisor both use
   `C:\Program Files\DeepAI Code\DeepAI Code.exe`; if that changes, fix the constant in
   `tools/supervisor.py`.

## Health checks a human can run in 30 seconds

```powershell
python tools/supervisor.py --status      # last health snapshot, no side effects
python tools/night_runner.py --report    # MORNING.md
type state\qwen\decisions.jsonl          # what qwen did each cycle
type state\supervisor\NEEDS_HUMAN.md     # present only when something needs a person
```

## Stopping and resuming

```powershell
type nul > state\night\stop     # all workers idle, supervisor still watches
del state\night\stop            # resume
```

## Recovery after a long outage (you come back to a dead PC)

1. Log in. The startup cmd relaunches supervisor, qwen, night runner.
2. `python tools/supervisor.py --once` — it will report what is missing and fix tasks.
3. `python tools/night_runner.py --report` — see which nights ran; gaps are expected.
4. If `NEEDS_HUMAN.md` exists, it lists the issues in order; each maps to a row above.
5. Restart the agent thread; the 08:05 prompt reads `MORNING.md` and continues.

## What the night actually produces

Per night, ~10 cycles × 4 jobs: 15 min of 1 Hz sweep-rate series, 5 min of 20 Hz decoy reads,
a footprint snapshot, 5 min of 10 Hz protection churn. Artifacts in
`state/night/artifacts/<night>/`; summarised in `MORNING.md` and `state/night/last_night.json`.

## Honest expectations

- Data collection: ~90–95 % of nights, given power and no lock-screen reboot.
- Agent turns: ~85–90 % (provider + app).
- Autonomous *progress*: the harness can run experiments and regression tests; it cannot do
  interactive reverse engineering. Expect roughly **40–60 % of a human-paced 4 weeks**, concentrated
  in measurement, scanner work, and any test suite that already exists.
