"""Process control, HTTP client and per-check context for the Vaultline verifier.

Black box only: the service is started with the workspace's [run].command, observed
over HTTP on 127.0.0.1:8080, and stopped with SIGKILL to its process group.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

PORT = 8080
READY_TIMEOUT = 15.0
REQUEST_TIMEOUT = 10.0
SELF_TEST_TIMEOUT = 5.0

# §10 cumulative bounds per data directory (the verifier's own budget, test-plan P5).
MAX_WRITES = 60000
MAX_BODY_BYTES = 256 * 1024 * 1024
MAX_LARGE_BODIES = 8
LARGE_BODY = 1024 * 1024

# Statuses the spec allows (§2, §5, §9); anything else fails the check.
ALLOWED_STATUS = {200, 201, 400, 404, 405, 409, 413}


class CheckFailed(Exception):
    """The service behaved differently from the spec."""


class CheckTimeout(Exception):
    """The check ran past its deadline."""


class EscapedProcessGroup(Exception):
    """The service left processes or the port behind after SIGKILL of its group."""


class VerifierBug(Exception):
    """The verifier broke one of its own rules (e.g. a §10 bound)."""


def expect(cond, msg):
    if not cond:
        raise CheckFailed(msg)


def q(value) -> str:
    """Percent-encodes every byte outside the RFC 3986 unreserved set (§11)."""
    if isinstance(value, bytes):
        return quote(value, safe="")
    return quote(value.encode("utf-8"), safe="")


# ---------------------------------------------------------------- processes


def port_open() -> bool:
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
        return True
    except OSError:
        return False


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes whose process group is pgid."""
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                stat = f.read().decode("latin-1")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        state, pgrp = fields[0], int(fields[2])
        if pgrp == pgid and state != "Z":
            members.append(int(name))
    return members


class Service:
    """One data directory and the service processes started on it."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.data_dir = tempfile.mkdtemp(prefix="vaultline-", dir=ctx.run.data_root)
        self.proc = None
        self.lock = threading.Lock()
        self.starts = 0
        self.writes = 0
        self.body_bytes = 0
        self.large_bodies = 0

    def count_request(self, method: str, path: str, declared: int):
        """Accounts one request against the §10 cumulative bounds."""
        is_write = method in ("PUT", "DELETE")
        if is_write:
            self.writes += 1
            self.body_bytes += declared
        if declared > LARGE_BODY:
            self.large_bodies += 1
        if (self.writes > MAX_WRITES or self.body_bytes > MAX_BODY_BYTES
                or self.large_bodies > MAX_LARGE_BODIES):
            raise VerifierBug(f"§10 cumulative bound exceeded: writes={self.writes} "
                              f"bytes={self.body_bytes} large={self.large_bodies}")

    def spawn(self):
        """Starts the run command without waiting for readiness."""
        with self.lock:
            self.ctx.check_cancel()
            self._spawn()

    def _spawn(self):
        if self.proc is not None:
            raise VerifierBug("spawn while a service is running")
        if port_open():
            raise EscapedProcessGroup("port 8080 is in use before the start")
        env = dict(os.environ, VAULTLINE_PORT=str(PORT), VAULTLINE_DATA_DIR=self.data_dir)
        log = open(self.ctx.log_path, "ab")
        self.starts += 1
        log.write(f"--- start {self.starts} ---\n".encode())
        log.flush()
        self.proc = subprocess.Popen(self.ctx.run.command, shell=True, cwd=self.ctx.run.workspace,
                                     env=env, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.ctx.client.reset()

    def wait_ready(self):
        """Polls GET /health until 200 (§2), within 15 s of the start."""
        t0 = time.monotonic()
        limit = t0 + READY_TIMEOUT
        while True:
            self.ctx.check_cancel()
            if self.proc.poll() is not None:
                raise CheckFailed(f"the run command exited with {self.proc.returncode} before it was ready")
            now = time.monotonic()
            if now > limit:
                raise CheckFailed("not ready within 15 s of the start (§2)")
            if now > self.ctx.deadline:
                raise CheckTimeout("deadline while waiting for readiness")
            try:
                c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1.0)
                c.request("GET", "/health")
                r = c.getresponse()
                r.read()
                c.close()
                if r.status == 200:
                    return now - t0
                expect(r.status == 503, f"/health returned {r.status} before readiness")
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.02)

    def start(self):
        self.spawn()
        return self.wait_ready()

    def kill(self):
        """SIGKILL to the whole process group, then the kill self-test."""
        with self.lock:
            self._kill()

    def _kill(self):
        if self.proc is None:
            return
        pgid = self.proc.pid
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()
        self.proc = None
        self.ctx.client.reset()
        limit = time.monotonic() + SELF_TEST_TIMEOUT
        while True:
            members = group_members(pgid)
            if not members and not port_open():
                return
            if time.monotonic() > limit:
                raise EscapedProcessGroup(
                    f"after SIGKILL of group {pgid}: processes {members}, port open={port_open()}")
            time.sleep(0.02)

    def restart(self):
        self.kill()
        return self.start()

    def close(self):
        try:
            self.kill()
        finally:
            shutil.rmtree(self.data_dir, ignore_errors=True)


# ---------------------------------------------------------------- HTTP


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers  # lower-case names
        self.body = body

    def json_media_type(self):
        return self.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            raise CheckFailed(f"response body is not JSON: {self.body[:200]!r}")

    def error_code(self):
        doc = self.json()
        try:
            return doc["error"]["code"]
        except (KeyError, TypeError):
            raise CheckFailed(f"error body has no error.code: {doc!r}")

    def __repr__(self):
        return f"<{self.status} {self.body[:120]!r}>"


class Client:
    """Keep-alive HTTP/1.1 client, one connection per thread, never pipelined."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.local = threading.local()
        self.generation = 0

    def reset(self):
        self.generation += 1

    def _conn(self, timeout):
        c = getattr(self.local, "conn", None)
        if c is not None and (self.local.gen != self.generation
                              or time.monotonic() - self.local.used > 1.0):
            c.close()
            c = None
        if c is None:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
            self.local.conn = c
            self.local.gen = self.generation
            self.local.fresh = True
        c.timeout = timeout
        if c.sock is not None:
            c.sock.settimeout(timeout)
        return c

    def drop(self):
        c = getattr(self.local, "conn", None)
        if c is not None:
            c.close()
            self.local.conn = None

    def request(self, method, path, body=None, headers=None, check_status=True):
        self.ctx.check_cancel()
        timeout = min(REQUEST_TIMEOUT, self.ctx.remaining())
        if timeout <= 0:
            raise CheckTimeout("deadline")
        headers = dict(headers or {})
        if body is not None:
            headers.setdefault("Content-Length", str(len(body)))
        self.ctx.service.count_request(method, path, len(body) if body else 0)
        for attempt in (0, 1):
            c = self._conn(timeout)
            reused = not self.local.fresh
            self.local.fresh = False
            got_bytes = False
            try:
                c.request(method, path, body=body, headers=headers)
                r = c.getresponse()
                got_bytes = True
                data = r.read()
                status, hdrs = r.status, {k.lower(): v for k, v in r.getheaders()}
                if r.will_close:
                    c.close()
                    self.local.conn = None
                self.local.used = time.monotonic()
                break
            except socket.timeout:
                self.drop()
                raise CheckFailed(f"{method} {path[:80]}: no complete response within {timeout:.0f} s")
            except (OSError, http.client.HTTPException) as err:
                self.drop()
                # A keep-alive connection the service closed while idle (§4) is retried once.
                if attempt == 0 and reused and not got_bytes:
                    continue
                self.ctx.check_cancel()
                raise CheckFailed(f"{method} {path[:80]}: connection error {err!r}")
        if check_status:
            expect(status in ALLOWED_STATUS, f"{method} {path[:80]}: status {status} is not allowed by the spec")
        return Response(status, hdrs, data)

    # ---- endpoint helpers

    def create_bucket(self, b):
        return self.request("PUT", f"/buckets/{b}")

    def delete_bucket(self, b):
        return self.request("DELETE", f"/buckets/{b}")

    def set_versioning(self, b, status):
        r = self.request("PUT", f"/buckets/{b}/versioning", json.dumps({"status": status}).encode())
        expect(r.status == 200, f"versioning PUT {status} on {b}: {r!r}")
        return r

    def put(self, b, key, data=b"", ctype=None, raw_key=False):
        headers = {"Content-Type": ctype} if ctype else {}
        k = key if raw_key else q(key)
        return self.request("PUT", f"/buckets/{b}/object?key={k}", data, headers)

    def put_ok(self, b, key, data=b"", ctype=None):
        r = self.put(b, key, data, ctype)
        expect(r.status == 200, f"PUT {b}/{key!r}: {r!r}")
        doc = r.json()
        for field in ("key", "versionId", "etag", "size", "lastModified"):
            expect(field in doc, f"PUT response lacks {field}: {doc!r}")
        return doc

    def get(self, b, key, version=None, method="GET", raw_key=False, raw_version=None):
        """GET/HEAD object; raw_* values are sent already percent-encoded."""
        k = key if raw_key else q(key)
        path = f"/buckets/{b}/object?key={k}"
        if raw_version is not None:
            path += "&versionId=" + raw_version
        elif version is not None:
            path += "&versionId=" + q(version)
        return self.request(method, path)

    def delete(self, b, key, version=None, raw_version=None):
        path = f"/buckets/{b}/object?key={q(key)}"
        if raw_version is not None:
            path += "&versionId=" + raw_version
        elif version is not None:
            path += "&versionId=" + q(version)
        return self.request("DELETE", path)

    def delete_ok(self, b, key, version=None):
        r = self.delete(b, key, version)
        expect(r.status == 200, f"DELETE {b}/{key!r} versionId={version!r}: {r!r}")
        doc = r.json()
        expect(isinstance(doc.get("versionId"), str) and isinstance(doc.get("deleteMarker"), bool),
               f"DELETE response shape: {doc!r}")
        return doc

    def list_page(self, b, versions, prefix=None, delimiter=None, max_keys=None, token=None,
                  start_after=None):
        parts = []
        if prefix is not None:
            parts.append("prefix=" + q(prefix))
        if delimiter is not None:
            parts.append("delimiter=" + q(delimiter))
        if max_keys is not None:
            parts.append(f"maxKeys={max_keys}")
        if start_after is not None:
            parts.append("startAfter=" + q(start_after))
        if token is not None:
            parts.append("token=" + q(token))
        ep = "versions" if versions else "objects"
        r = self.request("GET", f"/buckets/{b}/{ep}" + ("?" + "&".join(parts) if parts else ""))
        expect(r.status == 200, f"listing {ep} of {b}: {r!r}")
        return parse_page(r.json(), versions)

    def list_all(self, b, versions, prefix=None, delimiter=None, max_keys=None, start_after=None,
                 max_pages=100000):
        """One complete iteration (§6.4); returns the merged item sequence and the pages."""
        items, pages, token = [], [], None
        while True:
            page = self.list_page(b, versions, prefix, delimiter, max_keys, token,
                                  start_after if token is None else None)
            pages.append(page)
            items.extend(page["items"])
            if not page["isTruncated"]:
                return items, pages
            expect(len(pages) < max_pages, "listing did not end")
            token = page["nextToken"]


def utf8_key(s):
    return s.encode("utf-8")


def parse_page(doc, versions):
    """Validates a listing page's shape (§6.3) and merges entries and prefixes in key order."""
    expect(isinstance(doc, dict), f"listing body is not an object: {doc!r}")
    for f in ("entries", "commonPrefixes", "isTruncated", "nextToken"):
        expect(f in doc, f"listing lacks {f}")
    expect(isinstance(doc["entries"], list) and isinstance(doc["commonPrefixes"], list), "listing arrays")
    expect(isinstance(doc["isTruncated"], bool), "isTruncated must be a boolean")
    if doc["isTruncated"]:
        expect(isinstance(doc["nextToken"], str), "nextToken must be a string when truncated")
    else:
        expect(doc["nextToken"] is None, "nextToken must be null when not truncated")
    entries = []
    for e in doc["entries"]:
        expect(isinstance(e, dict) and isinstance(e.get("key"), str)
               and isinstance(e.get("versionId"), str)
               and isinstance(e.get("lastModified"), str), f"entry shape: {e!r}")
        if versions:
            expect(isinstance(e.get("isLatest"), bool) and isinstance(e.get("deleteMarker"), bool),
                   f"version entry shape: {e!r}")
            if e["deleteMarker"]:
                expect(e.get("etag") is None and e.get("size") is None, f"marker entry: {e!r}")
                entries.append(("v", e["key"], e["versionId"], e["isLatest"], True, None, None))
            else:
                expect(isinstance(e.get("etag"), str) and isinstance(e.get("size"), int), f"entry: {e!r}")
                entries.append(("v", e["key"], e["versionId"], e["isLatest"], False, e["etag"], e["size"]))
        else:
            expect(isinstance(e.get("etag"), str) and isinstance(e.get("size"), int), f"entry: {e!r}")
            entries.append(("o", e["key"], e["versionId"], e["etag"], e["size"]))
    for cp in doc["commonPrefixes"]:
        expect(isinstance(cp, str), f"common prefix: {cp!r}")
    for seq in (entries, doc["commonPrefixes"]):
        keys = [utf8_key(x[1] if isinstance(x, tuple) else x) for x in seq]
        expect(all(a <= b for a, b in zip(keys, keys[1:])), "page items out of order")
    items = entries + [("cp", cp) for cp in doc["commonPrefixes"]]
    items.sort(key=lambda it: utf8_key(it[1]))  # stable: one key's versions keep their order
    lm = {(e["key"], e["versionId"]): e["lastModified"] for e in doc["entries"]}
    return {"items": items, "isTruncated": doc["isTruncated"], "nextToken": doc["nextToken"],
            "raw": doc, "lastModified": lm}


# ---------------------------------------------------------------- check context


class RunConfig:
    def __init__(self, workspace, command, out_dir, data_root):
        self.workspace = workspace
        self.command = command
        self.out_dir = out_dir
        self.data_root = data_root


class Context:
    """Everything one check uses: its deadline, its service and its client."""

    def __init__(self, run: RunConfig, check_id: str, deadline: float, seed: int):
        self.run = run
        self.check_id = check_id
        self.deadline = deadline
        self.seed = seed
        self.cancelled = threading.Event()
        self.log_path = os.path.join(run.out_dir, "logs", f"{check_id}.log")
        self.client = Client(self)
        self.service = Service(self)
        self.notes = []

    def remaining(self):
        return self.deadline - time.monotonic()

    def check_cancel(self):
        if self.cancelled.is_set():
            raise CheckTimeout("cancelled")
        if self.remaining() <= 0:
            raise CheckTimeout("deadline")

    def note(self, msg):
        self.notes.append(msg)

    def threads(self, fns):
        """Runs fns concurrently, each on its own connection; re-raises the first failure."""
        errors = []

        def wrap(fn):
            try:
                fn()
            except BaseException as err:  # noqa: BLE001 - reported to the caller
                errors.append(err)
            finally:
                self.client.drop()

        ts = [threading.Thread(target=wrap, args=(fn,), daemon=True) for fn in fns]
        for t in ts:
            t.start()
        for t in ts:
            t.join(max(0.0, self.remaining()) + 1)
        if any(t.is_alive() for t in ts):
            raise CheckTimeout("worker threads did not finish")
        if errors:
            raise errors[0]
