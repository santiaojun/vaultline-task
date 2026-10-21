"""F5 Buckets & versioning state (§3.1, §3.4, §5.1, §5.2, §7, §8). No key-DELETE before a restart."""

import json

from common import Sync, body, expect, rnd


def bucket_basics(ctx):
    s = Sync(ctx)
    c = s.c
    for name in ("abc", "a" * 63, "0-9", "x-y-z", "mid-dle"):
        r = c.create_bucket(name)
        expect(r.status == 201 and r.json() == {"name": name}, f"create {name!r}: {r!r}")
        s.m.create(name)
    r = c.create_bucket("abc")
    expect(r.status == 409 and r.error_code() == "BucketAlreadyExists", f"duplicate create: {r!r}")
    for bad in ("ab", "a" * 64, "-abc", "abc-", "aBc", "a_b", "a.b", "ab~c"):
        r = c.create_bucket(bad)
        expect(r.status == 400 and r.error_code() == "InvalidBucketName", f"create {bad!r}: {r!r}")
    r = c.request("GET", "/buckets")
    expect(r.json() == {"buckets": [{"name": n} for n in sorted(s.m.buckets)]}, f"GET /buckets: {r.json()!r}")
    r = c.request("GET", "/buckets/abc/versioning")
    expect(r.status == 200 and r.json().get("status") == "Unversioned", f"initial state {r!r}")
    for raw in (b"", b"[]", b"null", b'"Enabled"', b'{"status":"enabled"}', b'{"status":"Unversioned"}',
                b"{}", b"{bad", b'{"status":1}', b'{"status":null}'):
        r = c.request("PUT", "/buckets/abc/versioning", raw)
        expect(r.status == 400 and r.error_code() == "InvalidArgument", f"versioning body {raw!r}: {r!r}")
    r = c.request("PUT", "/buckets/abc/versioning", json.dumps({"status": "Enabled", "note": [1]}).encode(),
                  {"Content-Type": "text/plain"})
    expect(r.status == 200 and r.json().get("status") == "Enabled", f"extra fields ignored: {r!r}")
    for st in ("Enabled", "Suspended", "Suspended", "Enabled"):
        r = c.request("PUT", "/buckets/abc/versioning", json.dumps({"status": st}).encode())
        expect(r.status == 200 and r.json().get("status") == st, f"set {st}: {r!r}")
        expect(c.request("GET", "/buckets/abc/versioning").json().get("status") == st, f"GET after {st}")
    r = c.delete_bucket("x-y-z")
    expect(r.status == 200 and r.json() == {"name": "x-y-z"}, f"delete bucket: {r!r}")
    r = c.delete_bucket("x-y-z")
    expect(r.status == 404 and r.error_code() == "NoSuchBucket", f"delete missing bucket: {r!r}")
    r = c.request("GET", "/buckets/x-y-z/versioning")
    expect(r.status == 404 and r.error_code() == "NoSuchBucket", f"versioning of a deleted bucket: {r!r}")
    s.m.delete("x-y-z")
    r = c.request("GET", "/buckets")
    expect(r.json() == {"buckets": [{"name": n} for n in sorted(s.m.buckets)]}, "GET /buckets after delete")


def delete_guard(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    s.create("guard-mk", "Enabled")
    s.delete_key("guard-mk", "ghost")             # only a marker
    s.create("guard-nc", "Enabled")
    s.put("guard-nc", "old", body(r, 1, 9))
    s.put("guard-nc", "old", body(r, 1, 9))
    s.delete_key("guard-nc", "old")               # only noncurrent versions under a marker
    for b in ("guard-mk", "guard-nc"):
        rr = c.delete_bucket(b)
        expect(rr.status == 409, f"delete non-empty {b}: {rr!r}")
        expect(c.request("GET", f"/buckets/{b}/versioning").status == 200, f"{b} still exists")
    for b in ("guard-mk", "guard-nc"):
        for k in list(s.m[b].keys):
            for v in list(s.m[b].history(k))[::-1]:    # oldest first
                s.delete_version(b, k, v.id)
        s.delete_bucket(b)
    expect(c.request("GET", "/buckets").json() == {"buckets": []}, "buckets remain")


def recreate_clean(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    b = "again-b"
    for rounds in range(2):
        s.create(b, "Enabled")
        ids = [(k, s.put(b, k, body(r, 1, 9))) for k in ("ra", "rb", "ra")]
        for k, vid in ids[::-1]:
            s.delete_version(b, k, vid)
        s.delete_bucket(b)
        if rounds == 1:
            ctx.service.restart()
        s.create(b)
        expect(c.request("GET", f"/buckets/{b}/versioning").json().get("status") == "Unversioned",
               "re-created bucket is not Unversioned")
        for versions in (False, True):
            items, _ = c.list_all(b, versions)
            expect(items == [], f"re-created bucket lists old items: {items}")
        for k, vid in ids:
            g = c.get(b, k, vid)
            expect(g.status == 404, f"old version {vid} visible in the re-created bucket: {g!r}")
        ctx.service.restart()
        expect(c.list_all(b, True)[0] == [], "old items after a restart")
        s.delete_bucket(b)


def bucket_linearizable(ctx):
    c = ctx.client
    for rnd_i in range(5):
        name = f"race-{rnd_i}"
        out = []
        ctx.threads([lambda: out.append(c.create_bucket(name).status) for _ in range(12)])
        expect(sorted(out) == [201] + [409] * 11, f"concurrent creates of {name}: {sorted(out)}")
        out = []
        ctx.threads([lambda: out.append(c.delete_bucket(name).status) for _ in range(12)])
        expect(sorted(out) == [200] + [404] * 11, f"concurrent deletes of {name}: {sorted(out)}")
    expect(c.request("GET", "/buckets").json() == {"buckets": []}, "buckets after the races")


def state_restart(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    s.create("st-en", "Enabled")
    s.create("st-su", "Suspended")
    s.create("st-un")
    s.create("st-gone")
    s.put("st-en", "x", body(r, 1, 9))
    s.delete_bucket("st-gone")
    for _ in range(2):
        ctx.service.restart()
        expect(c.request("GET", "/buckets").json() == {"buckets": [{"name": n} for n in sorted(s.m.buckets)]},
               "bucket set after restart")
        for b, bucket in s.m.buckets.items():
            got = c.request("GET", f"/buckets/{b}/versioning").json().get("status")
            expect(got == bucket.status, f"{b}: state {got}, want {bucket.status}")
        expect(c.request("GET", "/buckets/st-gone/versioning").status == 404, "deleted bucket answers")
        expect(c.request("GET", "/buckets/st-gone/objects").status == 404, "deleted bucket lists")
        s.versioning("st-un", "Suspended")
        s.versioning("st-su", "Enabled")
        s.expect_listing("st-en", True)
