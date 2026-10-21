"""F7 Concurrency (§4, §7). 16 connections at most (§10)."""

import hashlib
import socket
import threading

from common import body, expect, q, rnd
from harness import PORT


def _etag(data):
    return '"' + hashlib.md5(data).hexdigest() + '"'


def concurrent_puts(ctx):
    c = ctx.client
    expect(c.create_bucket("conc-b").status == 201, "create")
    c.set_versioning("conc-b", "Enabled")
    got = {}
    lock = threading.Lock()

    def worker(w):
        mine = []
        for n in range(8):
            data = f"w{w}-n{n}".encode() * 20
            vid = c.put_ok("conc-b", "hot", data)["versionId"]
            mine.append((vid, _etag(data)))
            c.put_ok("conc-b", f"own-{w:02d}", data)
        with lock:
            got[w] = mine

    ctx.threads([lambda w=w: worker(w) for w in range(16)])
    items, _ = c.list_all("conc-b", True, prefix="hot")
    hist = [(it[2], it[5]) for it in items]
    all_ids = [vid for mine in got.values() for vid, _ in mine]
    expect(len(set(all_ids)) == 16 * 8, "concurrent PUTs returned duplicate version IDs")
    expect(sorted(hist) == sorted(x for mine in got.values() for x in mine), "a version was lost or changed")
    order = {vid: i for i, (vid, _) in enumerate(hist)}
    for mine in got.values():
        idx = [order[vid] for vid, _ in mine]
        expect(idx == sorted(idx, reverse=True), "a PUT acknowledged before another began is not older (§3.3)")
    own, _ = c.list_all("conc-b", False, prefix="own-")
    expect(len(own) == 16, f"distinct-key PUTs landed: {len(own)}")


def null_race(ctx):
    c = ctx.client
    r = rnd(ctx)
    for name, state in (("race-sus", "Suspended"), ("race-unv", None)):
        expect(c.create_bucket(name).status == 201, "create")
        if state:
            c.set_versioning(name, "Enabled")
            c.put_ok(name, "nr", b"older non-null")
            c.set_versioning(name, state)
        bodies = [bytes(r.getrandbits(8) for _ in range(r.randint(100, 3000))) for _ in range(16 * 6)]
        ctx.threads([lambda w=w: [c.put_ok(name, "nr", bodies[w * 6 + n]) for n in range(6)] for w in range(16)])
        items, _ = c.list_all(name, True, prefix="nr")
        nulls = [it for it in items if it[2] == "null"]
        expect(len(nulls) == 1, f"{name}: {len(nulls)} null versions after concurrent PUTs")
        g = c.get(name, "nr", "null")
        expect(g.status == 200 and g.body in bodies and _etag(g.body) == nulls[0][5]
               and g.headers.get("etag") == _etag(g.body), f"{name}: the null version is not one complete body")
        if state:
            expect(len(items) == 2, f"{name}: the older non-null version was not kept")


def read_during_write(ctx):
    c = ctx.client
    r = rnd(ctx)
    expect(c.create_bucket("rdw-b").status == 201, "create")
    c.set_versioning("rdw-b", "Enabled")
    bodies = [bytes([i]) * (1000 * (i + 1)) for i in range(40)]
    old_ids = [c.put_ok("rdw-b", "rw", bodies[i])["versionId"] for i in range(5)]
    stop = threading.Event()

    def writer():
        try:
            for i in range(5, 40):
                c.put_ok("rdw-b", "rw", bodies[i])
            for vid in old_ids[:4]:
                expect(c.delete("rdw-b", "rw", vid).status == 200, "delete-by-ID")
        finally:
            stop.set()

    def reader(by_id):
        n = 0
        while not stop.is_set() or n < 5:
            n += 1
            g = c.get("rdw-b", "rw", old_ids[n % 4] if by_id else None)
            if by_id and g.status == 404:
                continue
            expect(g.status == 200, f"GET during writes: {g.status}")
            expect(g.headers.get("etag") == _etag(g.body) and g.headers.get("content-length") == str(len(g.body)),
                   "headers and body of a GET belong to different versions (§7)")
            expect(g.body in bodies, "GET returned bytes that no PUT sent")

    ctx.threads([writer] + [lambda: reader(False)] * 4 + [lambda: reader(True)] * 3)


def client_abort(ctx):
    c = ctx.client
    expect(c.create_bucket("abort-b").status == 201, "create")
    declared = 100000
    ctx.service.count_request("PUT", "", declared)
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    sock.sendall((f"PUT /buckets/abort-b/object?key={q('cut')} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"Content-Length: {declared}\r\n\r\n").encode() + b"x" * (declared // 2))
    others = []

    def other(i):
        others.append(c.put_ok("abort-b", f"ok-{i}", b"y" * (i + 1))["etag"])

    def closer():
        sock.close()

    ctx.threads([closer] + [lambda i=i: other(i) for i in range(6)])
    expect(len(others) == 6, "concurrent PUTs on other keys failed")
    for _ in range(3):
        c.request("GET", "/buckets")
    expect(c.get("abort-b", "cut").status == 404, "an upload closed mid-body created a version (§4)")
    items, _ = c.list_all("abort-b", True)
    expect(sorted(it[1] for it in items) == [f"ok-{i}" for i in range(6)], f"listing after abort: {items}")
    ctx.service.restart()
    expect(c.get("abort-b", "cut").status == 404, "an aborted upload appeared after a restart")
    expect(len(c.list_all("abort-b", True)[0]) == 6, "other keys lost after a restart")
