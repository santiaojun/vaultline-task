"""NF Limits (§2 readiness, §5.3, §9, §10)."""

import hashlib
import json
import queue
import random
import socket
import threading

from common import Sync, body, expect, q, rnd
from harness import PORT, CheckFailed

MAX_BODY = 16777216


def _oversize(ctx, key, declared):
    """Sends a PUT declaring `declared` bytes and the whole body; returns (status, body)."""
    ctx.service.count_request("PUT", "", declared)
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=min(10, ctx.remaining()))
    sock.sendall((f"PUT /buckets/limits-b/object?key={q(key)} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"Content-Length: {declared}\r\n\r\n").encode())
    chunk = b"\0" * (1 << 20)

    def send():
        left = declared
        try:
            while left > 0:
                n = sock.send(chunk[:min(left, len(chunk))])
                left -= n
        except OSError:
            pass                                    # the service may close after an early response

    t = threading.Thread(target=send, daemon=True)
    t.start()
    data = b""
    while b"\r\n\r\n" not in data:
        part = sock.recv(65536)
        if not part:
            raise CheckFailed("connection closed without a response to an oversize PUT")
        data += part
    head, rest = data.split(b"\r\n\r\n", 1)
    status = int(head.split(b" ")[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        part = sock.recv(65536)
        if not part:
            break
        rest += part
    t.join(min(10, ctx.remaining()))
    sock.close()
    ctx.client.reset()
    return status, rest[:length]


def oversize_413(ctx):
    c = ctx.client
    expect(c.create_bucket("limits-b").status == 201, "create")
    for declared in (MAX_BODY + 1, 2 * MAX_BODY):
        status, raw = _oversize(ctx, f"big-{declared}", declared)
        expect(status == 413, f"Content-Length {declared}: status {status}, want 413")
        expect(json.loads(raw)["error"]["code"] == "EntityTooLarge", f"error body {raw[:100]!r}")


def oversize_guard(ctx):
    c = ctx.client
    expect(c.create_bucket("limits-b").status == 201, "create")
    c.set_versioning("limits-b", "Enabled")
    kept = c.put_ok("limits-b", "kept", b"kept body")
    _oversize(ctx, "kept", MAX_BODY + 1)
    _oversize(ctx, "fresh", 2 * MAX_BODY)
    items, _ = c.list_all("limits-b", True)
    expect([(it[1], it[2]) for it in items] == [("kept", kept["versionId"])], f"rejected PUT changed state: {items}")
    g = c.get("limits-b", "kept")
    expect(g.status == 200 and g.body == b"kept body", "the previous version is no longer current")
    expect(c.get("limits-b", "fresh").status == 404, "a rejected PUT created a key")


def scale_recovery(ctx):
    """design/template-migration.md 7.1: one bucket at the §10 per-bucket maxima."""
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    for i in range(10):
        s.create(f"scale-{i}")
    s.versioning("scale-0", "Enabled")
    keys = ["data/%03d/obj-%05d.bin" % (i % 50, i) for i in range(2500)]
    marker_keys = set(r.sample(range(1, 2402), 200))
    big_keys = set(r.sample(sorted(set(range(1, 2402)) - marker_keys), 7))
    base = bytes(random.Random(ctx.seed + 1).getrandbits(8) for _ in range(4096)) * (MAX_BODY // 4096)
    lock = threading.Lock()
    model = s.m["scale-0"]

    def put(k, data):
        doc = c.put_ok("scale-0", k, data, "application/vnd.vaultline-scale+octet; charset=binary")
        expect(doc["etag"] == '"' + hashlib.md5(data).hexdigest() + '"', "PUT ETag")
        with lock:
            s.new_id("scale-0", k, doc["versionId"])
            model.put(s.m, k, doc["versionId"], data)

    def delete(k):
        doc = c.delete_ok("scale-0", k)
        expect(doc["deleteMarker"], "Enabled key-DELETE")
        with lock:
            s.new_id("scale-0", k, doc["versionId"])
            model.delete_key(s.m, k, doc["versionId"])

    def small(i, n):
        rr = random.Random(ctx.seed * 7919 + i * 131 + n)
        return bytes(rr.getrandbits(8) for _ in range(rr.randint(0, 3072)))

    # Phase 1: first PUT of every key, the 100-version key, and all deletes; phase 2: the
    # remaining PUTs (including the 7 large bodies), so every key-DELETE precedes at least
    # 2,000 later writes before the SIGKILL.
    phase1, phase2 = [], []
    # Key 0: 100 object versions (§10 per key). Each of the 200 markers is the last write
    # to its key, so no key-DELETE hits a marker-current key and none is followed by a
    # write to the same key.
    phase1.append([("put", 0, n) for n in range(100)])
    for i in range(1, 2500):
        if i in marker_keys:
            phase1.append([("put", i, 0), ("del", i, 1)])
        elif i < 2402:
            phase1.append([("put", i, 0)])
            phase2.append([("put", i, 1)])
        else:
            phase1.append([("put", i, 0)])

    def run(jobs):
        qq = queue.Queue()
        for j in jobs:
            qq.put(j)

        def worker():
            while True:
                try:
                    job = qq.get_nowait()
                except queue.Empty:
                    return
                for op, i, n in job:
                    if op == "del":
                        delete(keys[i])
                    elif n == 1 and i in big_keys:
                        put(keys[i], i.to_bytes(4, "big") + base[4:])
                    else:
                        put(keys[i], small(i, n))
        ctx.threads([worker for _ in range(16)])

    run(phase1)
    run(phase2)
    expect(sum(len(h) for h in model.keys.values()) == 5000 and len(model.keys) == 2500, "VERIFIER: scale shape")
    ctx.service.kill()
    ctx.service.start()                         # wait_ready enforces the 15 s bound (§2)
    for versions in (False, True):
        for mk in (1000, 7):
            s.expect_listing("scale-0", versions, max_keys=mk)
    s.expect_listing("scale-0", False, prefix="data/", delimiter="/")
    rr = random.Random(ctx.seed + 2)
    entries = [(k, v) for k, h in model.keys.items() for v in h if not v.marker]
    sample = rr.sample(entries, 200) + [(k, v) for k, v in entries if v.size == MAX_BODY]
    for k, v in sample:
        g = c.get("scale-0", k, v.id)
        expect(g.status == 200 and g.body == v.body, f"GET {k} {v.id} after restart")
    for i in range(1, 10):
        expect(c.list_page(f"scale-{i}", True)["items"] == [], "other buckets")
