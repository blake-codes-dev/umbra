# Night report · 2026-11-24 · generated 2026-11-24T12:56:13

Jobs: 9 (6 ok, 3 failed) · spent 2106s · window day

## Signals
- protection sweeps: **48** in 60.0s · median gap **1.08s** · regions flipped 3966

## Jobs
- `observer` 0s → exit 1
- `observer` 60s → ok
- `observer` 0s → exit 1
- `observer` 61s → ok
- `observer` 61s → ok
- `observer` 901s → ok
- `observer` 61s → ok
- `observer` 901s → exit 1
- `observer` 62s → ok

## Next actions
1. a job failed → read state/night/runs.jsonl before queueing more

## Night notes (qwen, local)
The observer ran for 60 seconds with 48 sweeps. The median gap between events was 1.08 seconds. A total of 3966 regions were flipped during the run. One job exited with an error, while the rest completed successfully.

## Machine state
```json
{
 "observer": {
  "seconds": 60.0,
  "sweeps": 48,
  "median_gap": 1.08,
  "regions_flipped": 3966,
  "decoy_hits": 0,
  "cpu_ms": 124671.9
 },
 "decoy": null,
 "spent": 2106.3,
 "failures": 3
}
```
