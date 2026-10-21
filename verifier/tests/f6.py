"""F6 Crash durability (§8). Only acknowledged facts and listed <=> readable are judged:
never ID reuse, version order or which version is current after a delete-by-ID."""

import hashlib
import socket

from common import Sync, body, expect, q, rnd
from harness import PORT, CheckFailed


class Facts:
    """Acknowledged per-key facts: (key, versionId) -> ETag (None for a marker), plus removals."""

    def __init__(self):
        self.present = {}
        self.removed = set()

    def added(self, b, key, vid, etag):
        self.present[(b, key, vid)] = etag
        self.removed.discard((b, key, vid))

    def gone(self, b, key, vid):
        self.present.pop((b, key, vid), None)
        self.removed.add((b, key, vid))


def observe(ctx, b, sample=None):
    """Full version listing; every listed version answers GET by ID consistently (§8.4)."""
    c = ctx.client
    items, _ = c.list_all(b, True)
    listed = {}
    for it in items:
        expect((it[1], it[2]) not in listed, f"{b}: version {it[1]!r}/{it[2]} listed twice")
        listed[(it[1], it[2])] = it[5]
    check_items = items if sample is None else items[:sample]
    for it in check_items:
        g = c.get(b, it[1], it[2])
        if it[4]:
            expect(g.status == 405, f"listed marker {it[2]} answers {g.status}")
        else:
            expect(g.status == 200 and len(g.body) == it[6] and '"' + hashlib.md5(g.body).hexdigest() + '"' == it[5],
                   f"listed version {it[1]!r}/{it[2]} is not fully readable ({g.status})")
    return listed


def check_facts(ctx, facts, buckets):
    for b in buckets:
        listed = observe(ctx, b, sample=40)
        for (fb, key, vid), etag in facts.present.items():
            if fb == b:
                expect((key, vid) in listed and listed[(key, vid)] == etag,
                       f"acknowledged version {key!r}/{vid} lost or changed after SIGKILL")
        for (fb, key, vid) in facts.removed:
            if fb == b:
                expect((key, vid) not in listed, f"removed version {key!r}/{vid} came back after SIGKILL")


def ack_durable(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    facts = Facts()
    s.create("dur-a", "Enabled")
    s.create("dur-b")
    for rnd_i in range(3):
        for _ in range(6):
            data = body(r, 0, 2000) * r.choice([1, 1, 150])
            k = f"d{r.randint(0, 4)}"
            vid = c.put_ok("dur-a", k, data)["versionId"]
            facts.added("dur-a", k, vid, '"' + hashlib.md5(data).hexdigest() + '"')
        present = [f for f in facts.present if f[0] == "dur-a"]
        victim = present[r.randrange(len(present))]
        expect(c.delete("dur-a", victim[1], victim[2]).status == 200, "delete-by-ID")
        facts.gone(*victim)
        state = ["Enabled", "Suspended"][rnd_i % 2]
        c.set_versioning("dur-b", state)
        name = f"dur-new-{rnd_i}"
        expect(c.create_bucket(name).status == 201, "create")
        last = rnd_i % 3
        if last == 0:
            data = body(r, 1, 50)
            vid = c.put_ok("dur-a", "last", data)["versionId"]
            facts.added("dur-a", "last", vid, '"' + hashlib.md5(data).hexdigest() + '"')
        elif last == 1:
            expect(c.delete_bucket(name).status == 200, "delete bucket")
        ctx.service.restart()                        # SIGKILL right after the last acknowledgement
        check_facts(ctx, facts, ["dur-a"])
        got = c.request("GET", "/buckets/dur-b/versioning").json().get("status")
        expect(got == state, f"versioning state {got}, want {state}")
        names = [x["name"] for x in c.request("GET", "/buckets").json()["buckets"]]
        expect((name in names) == (last != 1), f"bucket {name} create/delete not durable: {names}")


def _raw_put(path, data, send_bytes):
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    head = (f"PUT {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(data)}\r\n"
            "Content-Type: application/x-inflight\r\n\r\n").encode()
    sock.sendall(head + data[:send_bytes])
    return sock


def inflight_atomic(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    b = "inflight-b"
    s.create(b, "Enabled")
    size = 4 * 1024 * 1024
    offsets = [0, 1024 * 1024 + r.randint(0, 9999), size - 1, size]
    for i, off in enumerate(offsets):
        key = f"if{i}"
        before = body(r, 1, 30)
        before_id = c.put_ok(b, key, before)["versionId"]
        data = bytes(r.getrandbits(8) for _ in range(256)) * (size // 256)
        ctx.service.count_request("PUT", "", len(data))
        sock = _raw_put(f"/buckets/{b}/object?key={q(key)}", data, off)
        ctx.service.kill()                            # SIGKILL with the PUT in flight
        sock.close()
        outcomes = []
        for _ in range(2):                            # the outcome must not change on another restart
            ctx.service.start()
            listed = observe(ctx, b)
            mine = sorted(vid for (k, vid) in listed if k == key)
            expect(before_id in mine, "the acknowledged earlier version was lost")
            new = [vid for vid in mine if vid != before_id]
            expect(len(new) <= 1, f"in-flight PUT produced {len(new)} versions")
            if new:
                expect(off == size, "a PUT whose body was cut off by SIGKILL was applied (§4, §8.3)")
                expect(listed[(key, new[0])] == '"' + hashlib.md5(data).hexdigest() + '"', "torn body")
            outcomes.append(tuple(new))
            g = c.get(b, key)
            want = data if new else before
            expect(g.status == 200 and g.body == want, "GET does not answer for the newest listed version")
            ctx.service.kill()
        expect(outcomes[0] == outcomes[1], f"outcome changed across restarts: {outcomes}")
        ctx.service.start()


def marker_ack_durable(ctx):
    c = ctx.client
    r = rnd(ctx)
    expect(c.create_bucket("mkdur-b").status == 201, "create")
    c.set_versioning("mkdur-b", "Enabled")
    for i in range(3):
        key = f"mk{i}"
        c.put_ok("mkdur-b", key, body(r, 1, 20))
        mid = c.delete_ok("mkdur-b", key)["versionId"]
        ctx.service.restart()                         # SIGKILL right after the acknowledgement
        items, _ = c.list_all("mkdur-b", True)
        mine = [it for it in items if it[1] == key]
        expect(any(it[2] == mid and it[4] for it in mine), f"acknowledged marker {mid} lost after SIGKILL")
        expect(c.get("mkdur-b", key).status == 404, "the marker is not current after SIGKILL")


def crash_sequence(ctx):
    c = ctx.client
    r = rnd(ctx)
    facts = Facts()
    buckets = ["crash-a", "crash-b"]
    for b in buckets:
        expect(c.create_bucket(b).status == 201, "create")
        c.set_versioning(b, "Enabled")
    keys = [f"c{i}" for i in range(8)]
    kills = 0
    forced_marker_kill = False
    op = 0
    while kills < 20:
        op += 1
        b = r.choice(buckets)
        k = r.choice(keys)
        x = r.random()
        mine = [f for f in facts.present if f[0] == b and f[1] == k]
        if x < 0.5 or not mine:
            data = body(r, 0, 300)
            vid = c.put_ok(b, k, data)["versionId"]
            facts.added(b, k, vid, '"' + hashlib.md5(data).hexdigest() + '"')
            kind = "put"
        elif x < 0.75:
            mid = c.delete_ok(b, k)["versionId"]
            facts.added(b, k, mid, None)
            kind = "marker"
        else:
            # delete-by-ID of a non-current version only (never a current one)
            listed = c.list_page(b, True, prefix=k)["items"]
            older = [it for it in listed if it[1] == k and not it[3]]
            if not older:
                continue
            victim = r.choice(older)
            expect(c.delete(b, k, victim[2]).status == 200, "delete-by-ID")
            facts.gone(b, k, victim[2])
            kind = "delete"
        if op % 12 == 0 or (kind == "marker" and not forced_marker_kill):
            forced_marker_kill |= kind == "marker"
            kills += 1
            if kills == 7:
                ctx.service.kill()
                ctx.service.spawn()                   # SIGKILL during startup and recovery
            ctx.service.restart()
            check_facts(ctx, facts, buckets)
            if kills % 5 == 0:                        # revealed state is stable over another restart
                snap = [c.list_all(b2, True)[0] for b2 in buckets]
                ctx.service.restart()
                expect(snap == [c.list_all(b2, True)[0] for b2 in buckets], "state changed across a restart")
    expect(forced_marker_kill, "VERIFIER: no kill right after a key-DELETE acknowledgement")
    if ctx.service.writes > 300:
        raise CheckFailed(f"VERIFIER: {ctx.service.writes} writes exceed the 300-record restart rule")
