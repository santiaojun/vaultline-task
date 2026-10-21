# Vaultline mutants

Each patch applies independently to a fresh copy of `reference-solution/`. Apply it from the copy's root with `git apply /path/to/verifier/mutants/<name>.patch`, then grade that copy with the verifier (`WORKSPACE=<copy> TASK_DIR=<task root> OUT_DIR=<out> python3 verifier/tests/run_tests.py`).

**Rule.** A mutant's score is 100 minus the points of its target set, within 2 points. The target set is the explicit list of check IDs below, all inside one functionality. Each patch is one realistic defect and fails on behaviour, never on the build; the reference contains no code that exists only for a mutant.

**Shared integration check.** `X-integration` (1 point) is a seeded sequence across features and restarts. It is outside every mutant's target set. The semantic mutants #1–#7 also fail it by design, so their observed drop is the target points + 1, inside the ±2 tolerance.

## Mutants

| # | Patch | Functionality | Defect | Target checks | Target points | Expected score |
|--:|---|---|---|---|--:|--:|
| 1 | `cursor-by-version-id.patch` | F4 | The version-listing token stores the last entry's version ID, not its history position; if that ID is gone the rest of the key is skipped | `F4-n3-null-at-token`, `F4-cursor-delete-by-id`, `F4-n3-across-restart` (+ shared `X-integration`) | 9 | 90 |
| 2 | `marker-no-stack.patch` | F3 | An Enabled key-DELETE on a key whose current version is a marker returns that marker instead of adding a new one | `F3-marker-stacking`, `F3-marker-sequence` (+ shared `X-integration`) | 5 | 94 |
| 3 | `write-behind-delete.patch` | F6 | A key-DELETE is applied and acknowledged, and its record is appended 200 ms later | `F6-marker-ack-durable`, `F6-crash-sequence` (+ shared `X-integration`) | 3 | 96 |
| 4 | `seq-from-survivors.patch` | F2 | Recovery sets the next position to the highest surviving version's position | `F2-position-rebuild-restart`, `F2-ids-after-restart` (+ shared `X-integration`) | 7 | 92 |
| 5 | `null-replace-in-place.patch` | F2 | A Suspended or Unversioned PUT overwrites the null version where it sits, keeping its position | `F2-null-position`, `F2-suspended-put-replaces-null`, `F2-null-history-pages`, `F2-null-sequence` (+ shared `X-integration`) | 9 | 90 |
| 6 | `recompute-skips-markers.patch` | F3 | After the newest version is deleted by ID, GET and the objects listing show the newest object version as current, skipping markers above it | `F3-recompute-next-marker`, `F3-null-marker-delete-by-id`, `F3-marker-sequence` (+ shared `X-integration`) | 10 | 89 |
| 7 | `rollup-ignores-markers.patch` | F4 | The objects listing groups keys into common prefixes before skipping marker-current keys | `F4-marker-prefix-hidden`, `F4-marker-only-both-listings`, `F4-undelete-relists-prefix` (+ shared `X-integration`) | 5 | 94 |
| 8 | `commit-on-close.patch` | F7 | An upload whose client closes mid-body is committed with the bytes received | `F7-client-abort` | 1 | 99 |
| 10 | `no-size-limit.patch` | NF | No 413: an oversize Content-Length is accepted and stored | `NF-oversize-413`, `NF-oversize-guard` | 2 | 98 |
| 11 | `recovery-quadratic.patch` | NF | Replay re-reads and re-parses the whole log for every record (readiness 106 s at the scale check, 7.1x the 15 s bound) | `NF-scale-recovery` | 1 | 99 |

## Verification record

One row per run. *Failed = target* is yes when the failed checks are exactly the target set (plus `X-integration` for #1–#7). Evidence: `verifications/<patch>/<run>/reward.json`.

| # | Patch | Run | Target checks | Target points | Score | Observed drop | Drop − target | Failed = target |
|--:|---|--:|---|--:|--:|--:|--:|---|
| 1 | `cursor-by-version-id` | 1 | `F4-n3-null-at-token`, `F4-cursor-delete-by-id`, `F4-n3-across-restart` | 9 | 90 | 10 | +1 | yes |
| 1 | `cursor-by-version-id` | 2 | `F4-n3-null-at-token`, `F4-cursor-delete-by-id`, `F4-n3-across-restart` | 9 | 90 | 10 | +1 | yes |
| 2 | `marker-no-stack` | 1 | `F3-marker-stacking`, `F3-marker-sequence` | 5 | 94 | 6 | +1 | yes |
| 2 | `marker-no-stack` | 2 | `F3-marker-stacking`, `F3-marker-sequence` | 5 | 94 | 6 | +1 | yes |
| 3 | `write-behind-delete` | 1 | `F6-marker-ack-durable`, `F6-crash-sequence` | 3 | 96 | 4 | +1 | yes |
| 3 | `write-behind-delete` | 2 | `F6-marker-ack-durable`, `F6-crash-sequence` | 3 | 96 | 4 | +1 | yes |
| 4 | `seq-from-survivors` | 1 | `F2-position-rebuild-restart`, `F2-ids-after-restart` | 7 | 92 | 8 | +1 | yes |
| 4 | `seq-from-survivors` | 2 | `F2-position-rebuild-restart`, `F2-ids-after-restart` | 7 | 92 | 8 | +1 | yes |
| 5 | `null-replace-in-place` | 1 | `F2-null-position`, `F2-suspended-put-replaces-null`, `F2-null-history-pages`, `F2-null-sequence` | 9 | 90 | 10 | +1 | yes |
| 5 | `null-replace-in-place` | 2 | `F2-null-position`, `F2-suspended-put-replaces-null`, `F2-null-history-pages`, `F2-null-sequence` | 9 | 90 | 10 | +1 | yes |
| 6 | `recompute-skips-markers` | 1 | `F3-recompute-next-marker`, `F3-null-marker-delete-by-id`, `F3-marker-sequence` | 10 | 89 | 11 | +1 | yes |
| 6 | `recompute-skips-markers` | 2 | `F3-recompute-next-marker`, `F3-null-marker-delete-by-id`, `F3-marker-sequence` | 10 | 89 | 11 | +1 | yes |
| 7 | `rollup-ignores-markers` | 1 | `F4-marker-prefix-hidden`, `F4-marker-only-both-listings`, `F4-undelete-relists-prefix` | 5 | 94 | 6 | +1 | yes |
| 7 | `rollup-ignores-markers` | 2 | `F4-marker-prefix-hidden`, `F4-marker-only-both-listings`, `F4-undelete-relists-prefix` | 5 | 94 | 6 | +1 | yes |
| 8 | `commit-on-close` | 1 | `F7-client-abort` | 1 | 99 | 1 | +0 | yes |
| 8 | `commit-on-close` | 2 | `F7-client-abort` | 1 | 99 | 1 | +0 | yes |
| 10 | `no-size-limit` | 1 | `NF-oversize-413`, `NF-oversize-guard` | 2 | 98 | 2 | +0 | yes |
| 10 | `no-size-limit` | 2 | `NF-oversize-413`, `NF-oversize-guard` | 2 | 98 | 2 | +0 | yes |
| 11 | `recovery-quadratic` | 1 | `NF-scale-recovery` | 1 | 99 | 1 | +0 | yes |
| 11 | `recovery-quadratic` | 2 | `NF-scale-recovery` | 1 | 99 | 1 | +0 | yes |

## Controls

| Run | Score |
|---|--:|
| `control/1` (reference) | 100 |
| `control/2` (reference) | 100 |
| `control/3` (reference) | 100 |
| `control-cpu-load/1` (reference, 2 CPUs both kept busy) | 100 |
| `control-cpu-load/2` (reference, 2 CPUs both kept busy) | 100 |
| `control-docker/1` (reference in the task image, 2 CPUs, 1 GiB, no network) | 100 |
| `control-docker/2` (reference in the task image, 2 CPUs, 1 GiB, no network) | 100 |
| `control-docker/3` (reference in the task image, 2 CPUs, 1 GiB, no network) | 100 |
| `scaffold/1` (untouched `app/` scaffold (must score 0)) | 0 |
| `scaffold-docker/1` (untouched `app/` scaffold in the task image) | 0 |

The patches are absent from the candidate-visible materials. Re-run every record after changing the reference, the verifier or a patch.
