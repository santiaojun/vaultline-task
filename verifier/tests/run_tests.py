#!/usr/bin/env python3
"""Black-box verifier for Vaultline (instruction.md). Python 3.11 standard library only."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
from harness import CheckFailed, CheckTimeout, EscapedProcessGroup, VerifierBug  # noqa: E402

NODE_VERSION = "v20.20.2"
BASE_SEED = 20261020
PREFLIGHT_BUDGET = 10
TIMEOUT_BUDGET = 770      # per-check timeouts + preflight
HARD_STOP = 780           # seconds after the verifier starts
TEARDOWN_RESERVE = 5      # kill + self-test, inside each check's timeout
MIN_FREE_BYTES = 4 * 1024 ** 3

# (id, title, points, timeout s, module.function)
TESTS = [
    ("F1-object-basics", "PUT, GET and HEAD round-trip bodies, ETag, size and content type", 1, 8, "f1.object_basics"),
    ("F1-key-mapping", "keys are opaque byte strings: path-like, long and similar keys stay distinct", 2, 10, "f1.key_mapping"),
    ("F1-bom-key", "keys with U+FEFF, U+FFFF and supplementary code points round-trip intact", 1, 8, "f1.bom_key"),

    ("F2-id-rules", "version IDs follow the versioning state rules", 1, 8, "f2.id_rules"),
    ("F2-commit-order", "history order is commit order and lastModified never decreases", 1, 8, "f2.commit_order"),
    ("F2-no-reuse-after-delete", "an ID removed by delete-by-ID is never returned again", 1, 8, "f2.no_reuse_after_delete"),
    ("F2-null-position", "the null version keeps its place across versioning state changes", 1, 8, "f2.null_position"),
    ("F2-suspended-put-replaces-null", "a Suspended PUT replaces the null version and becomes newest", 3, 10, "f2.suspended_put_replaces_null"),
    ("F2-null-history-pages", "null histories read through delimited, paginated version listings", 3, 10, "f2.null_history_pages"),
    ("F2-null-sequence", "seeded PUT, versioning and delete-by-ID sequence matches the model", 2, 12, "f2.null_sequence"),
    ("F2-unversioned-null-after-enable", "an Unversioned null version stays in place after Enabled", 3, 10, "f2.unversioned_null_after_enable"),
    ("F2-null-delete-by-id", "deleting the null version by ID keeps the other versions", 2, 10, "f2.null_delete_by_id"),
    ("F2-position-rebuild-restart", "after deleting the newest versions and a restart, a PUT is newest with a new ID", 4, 12, "f2.position_rebuild_restart"),
    ("F2-ids-after-restart", "versions after restarts are newer and never reuse an ID", 3, 12, "f2.ids_after_restart"),

    ("F3-marker-responses", "delete marker status codes, headers and DELETE response bodies", 1, 8, "f3.marker_responses"),
    ("F3-marker-hides", "a delete marker hides the key from GET and the objects listing", 1, 8, "f3.marker_hides"),
    ("F3-older-readable", "versions under a delete marker stay readable by ID", 1, 8, "f3.older_readable"),
    ("F3-marker-stacking", "repeated key-DELETEs add new delete markers", 2, 10, "f3.marker_stacking"),
    ("F3-marker-sequence", "seeded PUT, key-DELETE and delete-by-ID sequence matches the model", 3, 12, "f3.marker_sequence"),
    ("F3-recompute-next-marker", "deleting the current version by ID makes a marker below it current", 4, 10, "f3.recompute_next_marker"),
    ("F3-null-marker-delete-by-id", "deleting a null marker by ID exposes the next-newest version", 3, 10, "f3.null_marker_delete_by_id"),
    ("F3-current-after-delete-by-id", "deleting current object versions by ID promotes the next one", 2, 10, "f3.current_after_delete_by_id"),
    ("F3-undelete", "removing the current marker by ID restores the key", 3, 10, "f3.undelete"),
    ("F3-marker-only-versions", "keys with only markers appear only in the version listing", 2, 10, "f3.marker_only_versions"),
    ("F3-suspended-delete", "a Suspended key-DELETE replaces the null version with a null marker", 1, 8, "f3.suspended_delete"),
    ("F3-long-token", "a long continuation token from a long delimiter is accepted", 1, 8, "f3.long_token"),

    ("F4-params-and-shape", "listing parameters, errors, field shapes, delimiters and byte order", 2, 8, "f4.params_and_shape"),
    ("F4-no-duplicates", "quiet pagination never repeats an item", 1, 10, "f4.no_duplicates"),
    ("F4-no-omissions", "quiet pagination lists every item with exact truncation", 1, 10, "f4.no_omissions"),
    ("F4-n3-null-at-token", "replacing the null version at the token place continues correctly", 3, 10, "f4.n3_null_at_token"),
    ("F4-n3-null-before-token", "replacing the null version before the token place continues correctly", 3, 10, "f4.n3_null_before_token"),
    ("F4-cursor-delete-by-id", "delete-by-ID between pages causes no skip and no repeat", 3, 10, "f4.cursor_delete_by_id"),
    ("F4-n3-across-restart", "a token survives a restart and a null replacement", 3, 12, "f4.n3_across_restart"),
    ("F4-token-across-restart", "a token survives a restart and a new version on its key", 2, 12, "f4.token_across_restart"),
    ("F4-marker-prefix-hidden", "a common prefix with only marker-current keys is hidden in the objects listing", 2, 10, "f4.marker_prefix_hidden"),
    ("F4-marker-only-both-listings", "marker-only keys under prefixes in both listings", 2, 10, "f4.marker_only_both_listings"),
    ("F4-undelete-relists-prefix", "removing the only current marker re-lists the common prefix", 1, 10, "f4.undelete_relists_prefix"),
    ("F4-churn-no-duplicates", "pagination during concurrent writes never repeats an item", 1, 15, "f4.churn_no_duplicates"),
    ("F4-churn-no-omissions", "pagination during concurrent writes lists every stable item once", 1, 15, "f4.churn_no_omissions"),

    ("F5-bucket-basics", "bucket create, list, delete and versioning endpoints", 1, 8, "f5.bucket_basics"),
    ("F5-delete-guard", "a bucket with markers or noncurrent versions cannot be deleted", 2, 10, "f5.delete_guard"),
    ("F5-recreate-clean", "a re-created bucket is empty and Unversioned", 2, 12, "f5.recreate_clean"),
    ("F5-bucket-linearizable", "concurrent creates of one name: exactly one succeeds", 1, 10, "f5.bucket_linearizable"),
    ("F5-state-restart", "buckets and versioning states survive a restart", 2, 12, "f5.state_restart"),

    ("F6-ack-durable", "acknowledged writes survive SIGKILL", 1, 12, "f6.ack_durable"),
    ("F6-inflight-atomic", "a PUT in flight at SIGKILL is absent or complete, and stays so", 2, 25, "f6.inflight_atomic"),
    ("F6-marker-ack-durable", "an acknowledged key-DELETE survives SIGKILL", 1, 12, "f6.marker_ack_durable"),
    ("F6-crash-sequence", "seeded writes with SIGKILLs keep every acknowledged effect", 2, 45, "f6.crash_sequence"),

    ("F7-concurrent-puts", "concurrent PUTs create distinct versions and lose none", 1, 12, "f7.concurrent_puts"),
    ("F7-null-race", "concurrent null PUTs leave one complete null version", 1, 12, "f7.null_race"),
    ("F7-read-during-write", "reads during writes return one complete version", 1, 12, "f7.read_during_write"),
    ("F7-client-abort", "an upload closed mid-body creates nothing", 1, 10, "f7.client_abort"),

    ("F8-errors-and-headers", "error bodies, codes, precedence and header names", 1, 8, "f8.errors_and_headers"),

    ("X-integration", "seeded mixed sequence across features with restarts matches the model", 1, 25, "x.integration"),

    ("NF-oversize-413", "oversize Content-Length is rejected with 413", 1, 10, "nf.oversize_413"),
    ("NF-oversize-guard", "a rejected oversize PUT changes nothing", 1, 10, "nf.oversize_guard"),
    ("NF-scale-recovery", "at full data scale, recovery is ready within 15 s and serves correctly", 1, 124, "nf.scale_recovery"),
]


def seed_for(check_id: str) -> int:
    return BASE_SEED + zlib.crc32(check_id.encode())


def load_scoring(path: Path):
    checks = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("- id:"):
            checks.append([line.split(":", 1)[1].strip(), None])
        elif line.startswith("points:") and checks:
            checks[-1][1] = int(line.split(":", 1)[1])
    return [tuple(c) for c in checks]


class Results:
    def __init__(self, out: Path, metadata):
        self.out = out
        self.metadata = metadata
        self.state = {t[0]: (False, "did not run") for t in TESTS}
        self.log = []

    def set(self, check_id, passed, detail=""):
        self.state[check_id] = (passed, detail)

    def write(self):
        tests = []
        for check_id, title, points, _, _ in TESTS:
            passed, _ = self.state[check_id]
            tests.append({"id": check_id, "title": title, "score": points if passed else 0,
                          "max_score": points, "status": "passed" if passed else "failed",
                          "type": "deterministic"})
        result = {"schema_version": 1, "task": self.metadata["id"], "task_version": self.metadata["version"],
                  "score": sum(t["score"] for t in tests), "max_score": sum(t["max_score"] for t in tests),
                  "tests": tests}
        tmp = self.out / "reward.json.tmp"
        tmp.write_text(json.dumps(result, indent=2) + "\n")
        os.replace(tmp, self.out / "reward.json")
        lines = list(self.log)
        for check_id, (passed, detail) in self.state.items():
            if not passed:
                lines.append(f"FAIL {check_id}: {detail}")
        (self.out / "verifier.log").write_text("\n".join(lines) + "\n")
        return result


def environment_error(out: Path, msg: str) -> int:
    (out / "verifier.log").write_text(f"ENVIRONMENT_ERROR {msg}\n")
    print(f"[verifier] environment error: {msg}")
    return 2


def resolve(target: str):
    module, func = target.split(".")
    try:
        return getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError):
        return None


def run_check(run, results: Results, entry, hard_stop: float) -> bool:
    """Runs one check; returns False if the run must stop (escaped process group)."""
    check_id, _, _, timeout, target = entry
    fn = resolve(target)
    seed = seed_for(check_id)
    t0 = time.monotonic()
    end = min(t0 + timeout, hard_stop)
    ctx = harness.Context(run, check_id, end - TEARDOWN_RESERVE, seed)
    outcome = {"passed": False, "detail": "not implemented" if fn is None else "did not finish"}

    def body():
        try:
            if fn is None:
                return
            if not getattr(fn, "manual_start", False):
                ctx.service.start()
            fn(ctx)
            outcome.update(passed=True, detail="")
        except CheckFailed as err:
            outcome["detail"] = str(err)
        except CheckTimeout as err:
            outcome["detail"] = f"timeout ({err})"
        except EscapedProcessGroup as err:
            outcome.update(detail=f"service escaped its process group: {err}", escaped=True)
        except VerifierBug as err:
            outcome["detail"] = f"VERIFIER BUG: {err}"
        except Exception as err:  # noqa: BLE001 - an unexpected response shape fails the check
            outcome["detail"] = f"unexpected {type(err).__name__}: {err}"
            with open(ctx.log_path, "a") as f:
                f.write(traceback.format_exc())

    os.makedirs(os.path.dirname(ctx.log_path), exist_ok=True)
    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(max(0.0, ctx.deadline - time.monotonic()) + 0.5)
    if worker.is_alive():
        ctx.cancelled.set()
        try:
            ctx.service.kill()
        except EscapedProcessGroup as err:
            outcome.update(detail=f"service escaped its process group: {err}", escaped=True)
        worker.join(TEARDOWN_RESERVE - 1)
        outcome["passed"] = False
        if not outcome.get("escaped"):
            outcome["detail"] = "timeout"
    ctx.cancelled.set()
    try:
        ctx.service.close()
    except EscapedProcessGroup as err:
        outcome.update(passed=False, detail=f"service escaped its process group: {err}", escaped=True)
    elapsed = time.monotonic() - t0
    s = ctx.service
    results.log.append(f"{'PASS' if outcome['passed'] else 'FAIL'} {check_id} {elapsed:.1f}s seed={seed} "
                       f"starts={s.starts} writes={s.writes} body_bytes={s.body_bytes}"
                       + (f" notes={ctx.notes}" if ctx.notes else ""))
    results.set(check_id, outcome["passed"], outcome["detail"])
    return not outcome.get("escaped")


def main() -> int:
    started = time.monotonic()
    hard_stop = started + HARD_STOP
    workspace = Path(os.environ.get("WORKSPACE", "/work/candidate"))
    out = Path(os.environ.get("OUT_DIR", "/logs/verifier"))
    task_dir = Path(os.environ.get("TASK_DIR", HERE.parents[1]))
    out.mkdir(parents=True, exist_ok=True)
    metadata = tomllib.loads((task_dir / "task.toml").read_text())["metadata"]

    # Verifier self-consistency: these are verifier bugs, never candidate failures.
    total_timeouts = sum(t[3] for t in TESTS)
    assert total_timeouts + PREFLIGHT_BUDGET <= TIMEOUT_BUDGET, f"timeouts sum to {total_timeouts}"
    assert [(t[0], t[2]) for t in TESTS] == load_scoring(task_dir / "verifier" / "scoring.yml"), \
        "TESTS and scoring.yml differ"
    assert sum(t[2] for t in TESTS) == 100 and all(1 <= t[2] <= 5 for t in TESTS)
    import model
    model.self_check()

    # Environment preflight: a mismatch means rerun, not a score.
    try:
        node = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=PREFLIGHT_BUDGET).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return environment_error(out, f"node -v failed: {err}")
    if node != NODE_VERSION:
        return environment_error(out, f"node -v printed {node!r}, expected {NODE_VERSION}")
    if harness.port_open():
        return environment_error(out, "port 8080 is in use before the first check")
    data_root = tempfile.mkdtemp(prefix="vaultline-verifier-")
    if shutil.disk_usage(data_root).free < MIN_FREE_BYTES:
        return environment_error(out, "less than 4 GiB free for data directories")

    results = Results(out, metadata)
    results.log.append(f"verifier start; node {node}; base seed {BASE_SEED}")
    results.write()

    problem = ""
    command = ""
    setup_log = out / "setup.log"
    log_text = setup_log.read_text(errors="replace") if setup_log.exists() else ""
    if "setup skipped:" in log_text or "setup failed" in log_text:
        problem = "setup failed: " + log_text[-300:].strip()
    elif not workspace.is_dir():
        problem = f"workspace {workspace} does not exist"
    else:
        try:
            run_cfg = tomllib.loads((workspace / "config.toml").read_text())["run"]
            command = str(run_cfg["command"]).strip()
            if 8080 not in list(run_cfg.get("ports", [])):
                problem = "config.toml [run].ports must contain 8080"
        except (OSError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as err:
            problem = f"config.toml has no usable [run].command: {err}"
        if not command and not problem:
            problem = "config.toml [run].command is empty"
    if problem:
        for t in TESTS:
            results.set(t[0], False, problem)
        result = results.write()
        print(f"[verifier] score={result['score']}/{result['max_score']} ({problem})")
        return 0

    run = harness.RunConfig(str(workspace), command, str(out), data_root)
    for entry in TESTS:
        if time.monotonic() + 1 > hard_stop:
            results.log.append("global hard stop reached")
            break
        if shutil.disk_usage(data_root).free < MIN_FREE_BYTES:
            results.write()
            return environment_error(out, "less than 4 GiB free for data directories")
        ok = run_check(run, results, entry, hard_stop)
        results.write()
        if not ok:
            results.log.append("stopping: the service escaped its process group")
            for t in TESTS:
                if results.state[t[0]] == (False, "did not run"):
                    results.set(t[0], False, "did not run: an earlier check's service escaped its process group")
            break
    shutil.rmtree(data_root, ignore_errors=True)
    result = results.write()
    print(f"[verifier] score={result['score']}/{result['max_score']} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
