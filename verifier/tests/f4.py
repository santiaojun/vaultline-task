"""F4 Listing & pagination (§6). Enabled buckets only, except the N3 cases.

Delete-by-ID removes only non-current versions, except undelete over an object.
N3 checks compare only the first page and its continuations. Churn writers only add
and remove single-version keys.
"""

import threading

from common import Sync, body, expect, q, rnd
from harness import utf8_key


def _err(c, path):
    r = c.request("GET", path)
    expect(r.status == 400, f"GET {path[:80]}: want 400, got {r!r}")
    return r.error_code()


def params_and_shape(ctx):
    s = Sync(ctx)
    c = s.c
    b = "params-b"
    s.create(b, "Enabled")
    keys = ["g--h--i", "g--j", "gk", "r/s/t", "r/u", "w/1", "w/2", "wz", "Z", "z", "", "\U0001F600"]
    for k in keys:
        s.put(b, k, k.encode())
    base = f"/buckets/{b}/objects?"
    for bad in ("maxKeys=0", "maxKeys=1001", "maxKeys=01", "maxKeys=%2B1", "maxKeys=", "maxKeys=1x",
                "maxKeys=%FF", "prefix=%FF", "delimiter=%C0%80", "startAfter=%ED%A0%80"):
        expect(_err(c, base + bad) == "InvalidArgument", f"{bad}: want InvalidArgument")
    expect(_err(c, base + "token=") == "InvalidToken", "empty token")
    first = c.list_page(b, False, max_keys=2)
    tok = first["nextToken"]
    expect(_err(c, base + f"startAfter=a&token={q(tok)}") == "InvalidArgument", "token with startAfter")
    s.create("params-c", "Enabled")
    s.put("params-c", "x", b"x")
    for wrong in (f"/buckets/params-c/objects?maxKeys=2&token={q(tok)}",
                  f"/buckets/{b}/versions?maxKeys=2&token={q(tok)}",
                  f"/buckets/{b}/objects?prefix=g&token={q(tok)}",
                  f"/buckets/{b}/objects?delimiter=%2F&token={q(tok)}"):
        expect(_err(c, wrong) == "InvalidToken", f"token used with another binding: {wrong[:60]}")
    # Absent and empty prefix/delimiter are the same binding.
    r = c.request("GET", f"/buckets/{b}/objects?prefix=&delimiter=&maxKeys=2&token={q(tok)}")
    expect(r.status == 200, f"empty prefix/delimiter with a token issued without them: {r!r}")
    r = c.request("GET", f"/buckets/{b}/versions?startAfter=%FF")
    expect(r.status == 200, "startAfter is not used (nor decoded) on the version listing")
    # Field names and types are validated by parse_page; byte order, multi-character
    # delimiter, delimiter inside prefix and startAfter-then-roll-up against the model.
    s.expect_listing(b, False)
    s.expect_listing(b, True)
    s.expect_listing(b, False, delimiter="--")
    s.expect_listing(b, False, prefix="r/", delimiter="/")
    s.expect_listing(b, False, delimiter="/", start_after="w/1")
    s.expect_listing(b, True, delimiter="/", max_keys=3)
    items = s.expect_listing(b, False, start_after="w/1", delimiter="/")
    expect(("cp", "w/") in items, "a common prefix not greater than startAfter can appear (§6.2)")


def _quiet_bucket(s, ctx, b):
    r = rnd(ctx)
    s.create(b, "Enabled")
    names = []
    for i in range(18):
        names += [f"p{i % 3}/q{i}", f"p{i % 3}/q{i}/deep", f"t{i:02d}"]
    for k in names:
        for _ in range(1 + (len(k) % 3)):
            s.put(b, k, body(r, 0, 8))
    return names


def no_duplicates(ctx):
    s = Sync(ctx)
    b = "nodup-b"
    _quiet_bucket(s, ctx, b)
    for versions in (False, True):
        for delim in (None, "/"):
            for mk in (1, 2, 7):
                items, _ = s.c.list_all(b, versions, delimiter=delim, max_keys=mk)
                ident = [it[:3] if it[0] != "cp" else it for it in items]
                expect(len(ident) == len(set(ident)), f"an item appears twice (maxKeys={mk}, delimiter={delim!r})")
                keys = [utf8_key(it[1]) for it in items]
                expect(all(a <= b2 for a, b2 in zip(keys, keys[1:])), "items not in increasing order across pages")
                if not versions:
                    expect(all(a < b2 for a, b2 in zip(keys, keys[1:])), "objects listing repeats a key")


def no_omissions(ctx):
    s = Sync(ctx)
    b = "noomit-b"
    _quiet_bucket(s, ctx, b)
    for versions in (False, True):
        for delim in (None, "/"):
            for mk in (1, 2, 7):
                s.paginate(b, versions, mk, delimiter=delim)


def _n3_bucket(s, ctx, b):
    """Key n3k gets the history [A, null, B, C] (newest first); n3z follows it."""
    r = rnd(ctx)
    s.create(b, "Enabled")
    s.put(b, "n3k", body(r, 1, 9))
    s.put(b, "n3k", body(r, 1, 9))
    s.versioning(b, "Suspended")
    s.put(b, "n3k", body(r, 1, 9))
    s.versioning(b, "Enabled")
    s.put(b, "n3k", body(r, 1, 9))
    s.put(b, "n3z", body(r, 1, 9))
    expect([v.id == "null" for v in s.m[b].history("n3k")] == [False, True, False, False], "VERIFIER: n3 history")
    return r


def _replace_null(s, b, r):
    def between(i):
        if i == 0:
            s.versioning(b, "Suspended")
            s.put(b, "n3k", body(r, 1, 9))
            s.versioning(b, "Enabled")
    return between


def n3_null_at_token(ctx):
    s = Sync(ctx)
    b = "n3a-b"
    r = _n3_bucket(s, ctx, b)
    s.paginate(b, True, 2, between=_replace_null(s, b, r))      # page 1 ends on null


def n3_null_before_token(ctx):
    s = Sync(ctx)
    b = "n3b-b"
    r = _n3_bucket(s, ctx, b)
    s.paginate(b, True, 3, between=_replace_null(s, b, r))      # page 1 ends after null


def cursor_delete_by_id(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "cursor-b"
    s.create(b, "Enabled")
    for _ in range(5):
        s.put(b, "cd", body(r, 1, 9))
    s.put(b, "ce", body(r, 1, 9))
    s.put(b, "ce", body(r, 1, 9))
    h = list(s.m[b].history("cd"))

    def between(i):
        if i == 0:                      # page 1 = [v5, v4, v3]
            s.delete_version(b, "cd", h[1].id)   # before the place, not current
            s.delete_version(b, "cd", h[2].id)   # at the place
    s.paginate(b, True, 3, between=between)


def n3_across_restart(ctx):
    s = Sync(ctx)
    b = "n3r-b"
    r = _n3_bucket(s, ctx, b)

    def between(i):
        if i == 0:
            ctx.service.restart()
            s.versioning(b, "Suspended")
            s.put(b, "n3k", body(r, 1, 9))
            s.versioning(b, "Enabled")
    s.paginate(b, True, 2, between=between)


def token_across_restart(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "tokr-b"
    s.create(b, "Enabled")
    for _ in range(4):
        s.put(b, "tk", body(r, 1, 9))
    s.put(b, "tz", body(r, 1, 9))

    def between(i):
        if i == 0:
            ctx.service.restart()
            s.put(b, "tk", body(r, 1, 9))       # newer than the place
    s.paginate(b, True, 2, between=between)
    s.paginate(b, False, 1, between=lambda i: ctx.service.restart() if i == 0 else None)


def marker_prefix_hidden(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "mph-b"
    s.create(b, "Enabled")
    for k in ("hid/a", "hid/b", "live/c", "mix/d", "mix/e"):
        s.put(b, k, body(r, 1, 9))
    for k in ("hid/a", "hid/b", "mix/d"):
        s.delete_key(b, k)
    items = s.expect_listing(b, False, delimiter="/")
    expect(("cp", "hid/") not in items and ("cp", "mix/") in items, f"objects roll-up {items}")
    vitems = s.expect_listing(b, True, delimiter="/")
    expect(("cp", "hid/") in vitems, "version listing keeps the prefix")
    s.expect_listing(b, False, delimiter="/", max_keys=1)


def marker_only_both_listings(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "mob-b"
    s.create(b, "Enabled")
    s.put(b, "keep/x", body(r, 1, 9))
    for k in ("only/a", "only/b", "only2/c", "solo"):
        s.delete_key(b, k)
    for versions in (False, True):
        for mk in (1, 2, 5):
            s.paginate(b, versions, mk, delimiter="/")
            s.paginate(b, versions, mk)
    for k in ("only/a", "only/b", "only2/c", "solo"):
        for v in list(s.m[b].history(k))[::-1]:     # oldest first
            s.delete_version(b, k, v.id)
    for versions in (False, True):
        s.expect_listing(b, versions, delimiter="/")


def undelete_relists_prefix(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "udp-b"
    s.create(b, "Enabled")
    s.put(b, "ud/x", body(r, 1, 9))
    s.put(b, "other", body(r, 1, 9))
    mid = s.delete_key(b, "ud/x")
    items = s.expect_listing(b, False, delimiter="/")
    expect(("cp", "ud/") not in items, "prefix of a marker-current key listed")
    s.delete_version(b, "ud/x", mid)           # next-newest is an object
    items = s.expect_listing(b, False, delimiter="/")
    expect(("cp", "ud/") in items, "prefix not re-listed after the marker was removed")


def _churn(ctx, check):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "churn-b"
    s.create(b, "Enabled")
    stable = [f"s{i:02d}" for i in range(30)]
    for k in stable:
        for _ in range(3):
            s.put(b, k, body(r, 0, 8))
    expected = [it for it, _ in s.m[b].sequence(True)]
    stable_objects = [it for it, _ in s.m[b].sequence(False)]
    done = threading.Event()
    c = ctx.client

    def writer(w):
        for n in range(25):
            k = f"s{(n * 7 + w) % 30:02d}-c{w}-{n}"    # sorts between stable keys
            rr = c.put(b, k, b"churn")
            expect(rr.status == 200, f"churn PUT {rr!r}")
            vid = rr.json()["versionId"]
            rd = c.delete(b, k, vid)
            expect(rd.status == 200, f"churn delete {rd!r}")

    def reader():
        rounds = 0
        while not done.is_set() or rounds < 2:
            for versions, mk in ((True, 7), (False, 5)):
                items, _ = c.list_all(b, versions, max_keys=mk)
                check(items, expected if versions else stable_objects, versions)
            rounds += 1
            if rounds > 50:
                break

    def writers():
        try:
            ctx.threads([lambda w=w: writer(w) for w in range(8)])
        finally:
            done.set()

    ctx.threads([writers, reader])


def churn_no_duplicates(ctx):
    def check(items, expected, versions):
        ident = [it[1:3] for it in items]
        expect(len(ident) == len(set(ident)), "an item appears twice during churn")
        keys = [utf8_key(it[1]) for it in items]
        expect(all(a <= b for a, b in zip(keys, keys[1:])), "items out of order across pages during churn")
    _churn(ctx, check)


def churn_no_omissions(ctx):
    def check(items, expected, versions):
        got = [it for it in items if "-c" not in it[1]]
        expect(got == expected, f"stable items missing or repeated during churn: {len(got)} vs {len(expected)}")
    _churn(ctx, check)
