# Night report · 2026-11-24 · generated 2026-11-24T12:01:08

Jobs: 7 (5 ok, 2 failed) · spent 1144s · window day

## Signals
- protection sweeps: **51** in 60.0s · median gap **1.06s** · regions flipped 897

## Jobs
- `observer` 0s → exit 1
- `observer` 60s → ok
- `observer` 0s → exit 1
- `observer` 61s → ok
- `observer` 61s → ok
- `observer` 901s → ok
- `observer` 61s → ok

## Next actions
1. a job failed → read state/night/runs.jsonl before queueing more

## Night notes (qwen, local)
The observer ran for 60 seconds with 51 sweeps. The median gap between events was 1.06 seconds. A total of 897 region flips occurred. One observer job exited with an error.

## Machine state
```json
{
 "observer": {
  "seconds": 60.0,
  "sweeps": 51,
  "median_gap": 1.06,
  "regions_flipped": 897,
  "decoy_hits": 0,
  "cpu_ms": 30687.5
 },
 "decoy": null,
 "spent": 1144.1,
 "failures": 2
}
```
