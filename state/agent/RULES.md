# RULES — operating protocol for every agent turn

Written for a future turn that has lost the conversation (compaction, app restart, new model,
new thread). Follow it literally; it exists because context is the scarcest resource.

## Start of turn (cheap)

1. `python tools/brief.py` — one call, one small file. Read `state/agent/BRIEF.md`.
2. Only open other files when the brief is insufficient. Never read a large file whole:
   `grep -n` first, then `read` with offset/limit.
3. Trust the brief over your memory of the conversation.

## Doing work

4. Batch shell work into one command (`&&`, heredocs) instead of five calls.
5. After every `write`/`edit`, verify: `ls -l` the path or `python -m py_compile`. A write can be
   reported as skipped if a user message arrives mid-turn; re-issue it once.
6. Prefer editing existing files. No new summary/report files unless the brief needs them.
7. Offline: no web tools, no network assumptions. Online: use the network, but never block on it.
8. No call cap. A turn continues while the queue has an open item and the context is not
   nearly full. Do not stop because a sub-task ended; stop only when the queue is empty,
   blocked, or the context is close to its limit. Write progress to next.json as you go
   so a compaction mid-turn cannot lose the thread.

## Failure protocol

9. **3 strikes:** the same operation failing three times → write `state/supervisor/NEEDS_HUMAN.md`
   with what you tried, then stop. Never loop.
10. Provider errors (429, 529, "too much", context overflow, model unavailable) → stop cleanly.
    The 3-hour watchdog retries; do not burn a turn retrying the provider.
11. A tool that does not exist → `deepai_tool_search` once, then fall back to bash. Do not guess
    names repeatedly.
12. If `state/mode.json` says offline, do not attempt web tools or Discord uploads.

## End of turn (mandatory)

13. Append one line to `state/agent/JOURNAL.md`: `YYYY-MM-DD HH:MM · did X · next Y`.
14. Overwrite `state/agent/next.json` with `{"next": "...", "blocked": [...], "updated": "..."}`.
15. Overwrite `state/agent/last_seen.json` with `{"at": "<iso local>", "turn": "<kind>"}`.
16. Update `PLAN.md` milestone status only when the proof exists. No optimistic ticks.

## Rules for the project itself

17. Never delete `baselines/`, `state/`, or `artifacts/` without a reason recorded in JOURNAL.md.
18. Scope lives in `PRODUCT.md`. Do not expand it mid-trip; the human is away.
19. Any change to the executor must be followed by `footprint_audit.py --diff` against the
    version-keyed baseline. Stealth claims without a diff are opinions.
20. When a Roblox update lands, patterns are re-validated before anything else that night.

## File boundary (hard)

21. Personal folders (Desktop, Documents, Downloads, Pictures, Videos, Music, OneDrive)
    are read-only for the automation. Only two tools write: guard.py (deletes, inside the
    project only) and vault.py (copies out to D:/LyphenVault). Never add a delete path that
    does not call guard.may_delete().
22. Local models choose action names, never paths or commands. If a new action needs a
    path, it is fixed in code, not proposed by the model.

## 24/7 loop (hard)

23. A turn is a shift, not a task. state/agent/QUEUE.md is the job: top unchecked item,
    finish or record the blocker, tick, next.
24. Do not end a turn because a sub-task finished; end only when the queue is empty,
    context is nearly full, or a blocker needs the human.
25. Interim messages end with <!-- deepai:work-remains --> on its own line.
