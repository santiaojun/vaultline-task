"""F2 Versions, null, IDs, positions (§3.3, §3.4, §8.5). No delete markers in this group."""

from common import Sync, body, check_id, expect, rnd


def id_rules(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    s.create("ids-en", "Enabled")
    ids = [s.put("ids-en", "obj", body(r)) for _ in range(5)]
    for vid in ids:
        check_id(vid)
    expect(len(set(ids)) == 5 and "null" not in ids, f"Enabled IDs {ids}")
    s.versioning("ids-en", "Suspended")
    expect(s.put("ids-en", "obj", body(r)) == "null", "Suspended PUT ID")
    s.create("ids-un")
    expect(s.put("ids-un", "obj", body(r)) == "null", "Unversioned PUT ID")
    s.expect_listing("ids-en", True)


def commit_order(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    s.create("order-b", "Enabled")
    for _ in range(30):
        s.put("order-b", "hot", body(r, 0, 16))
    s.expect_listing("order-b", True)
    page = s.c.list_page("order-b", True)
    lms = [page["lastModified"][(it[1], it[2])] for it in page["items"]]
    expect(all(a >= b for a, b in zip(lms, lms[1:])), "lastModified decreases from older to newer (§3.3)")


def no_reuse_after_delete(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    s.create("reuse-b", "Enabled")
    ids = [s.put("reuse-b", "item", body(r)) for _ in range(6)]
    s.delete_version("reuse-b", "item", ids[-1])
    s.delete_version("reuse-b", "item", ids[2])
    s.delete_version("reuse-b", "item", ids[-2])
    for _ in range(10):
        s.put("reuse-b", "item", body(r))       # Sync.new_id rejects any removed ID
    s.expect_listing("reuse-b", True)


def null_position(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "nullpos-b"
    s.create(b)
    steps = [None, "Enabled", None, None, "Suspended", None, "Enabled", None, "Suspended", None, None]
    for st in steps:
        if st:
            s.versioning(b, st)
        else:
            s.put(b, "np", body(r, 1, 20))
        s.expect_listing(b, True)
        s.expect_get(b, "np")
        s.expect_get(b, "np", "null")


def suspended_put_replaces_null(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "susput-b"
    s.create(b, "Enabled")
    s.put(b, "doc", body(r, 1, 30))
    s.versioning(b, "Suspended")
    s.put(b, "doc", b"first null body")
    s.versioning(b, "Enabled")
    s.put(b, "doc", body(r, 1, 30))
    s.put(b, "other", body(r, 1, 30))
    s.versioning(b, "Suspended")
    s.put(b, "doc", b"second null body")
    hist = [v.id for v in s.m[b].history("doc")]
    expect(hist[0] == "null" and hist.count("null") == 1 and len(hist) == 3, f"model history {hist}")
    s.expect_listing(b, True)
    s.expect_get(b, "doc")
    s.expect_get(b, "doc", "null")


def null_history_pages(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "nhp-b"
    s.create(b)
    keys = ["dir/a1", "dir/a2", "dir/sub/x", "dir/sub/y", "top", "zz/q"]
    for k in keys:
        s.put(b, k, body(r, 1, 10))
    s.versioning(b, "Enabled")
    for k in keys[::2]:
        s.put(b, k, body(r, 1, 10))
        s.put(b, k, body(r, 1, 10))
    s.versioning(b, "Suspended")
    for k in keys[:4]:
        s.put(b, k, body(r, 1, 10))     # null replaced below non-null versions on some keys
    s.versioning(b, "Enabled")
    s.put(b, keys[0], body(r, 1, 10))
    for mk in (1, 2, 3):
        s.expect_listing(b, True, delimiter="/", max_keys=mk)
        s.expect_listing(b, True, prefix="dir/", delimiter="/", max_keys=mk)
        s.expect_listing(b, True, max_keys=mk)


def null_sequence(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "nseq-b"
    s.create(b)
    keys = [f"n{i}" for i in range(6)]
    for step in range(200):
        x = r.random()
        k = r.choice(keys)
        bucket = s.m[b]
        if x < 0.08:
            s.versioning(b, r.choice(["Enabled", "Suspended"]))
        elif x < 0.75 or not bucket.history(k):
            s.put(b, k, body(r, 0, 24))
        else:
            s.delete_version(b, k, r.choice(bucket.history(k)).id)
        if step % 50 == 49:
            s.expect_listing(b, True)
            for key in keys:
                s.expect_get(b, key)


def unversioned_null_after_enable(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "unv-en-b"
    s.create(b)
    s.put(b, "one", b"unversioned era body")
    s.put(b, "two", body(r, 1, 20))
    s.versioning(b, "Enabled")
    s.put(b, "one", body(r, 1, 20))
    s.put(b, "one", body(r, 1, 20))
    s.expect_listing(b, True)
    s.expect_listing(b, False)
    s.expect_get(b, "one", "null")
    for v in list(s.m[b].history("one"))[:2]:
        s.delete_version(b, "one", v.id)
    s.expect_listing(b, True)
    s.expect_get(b, "one")


def null_delete_by_id(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "ndel-b"
    s.create(b, "Enabled")
    s.put(b, "nd", body(r, 1, 20))
    s.versioning(b, "Suspended")
    s.put(b, "nd", body(r, 1, 20))
    s.delete_version(b, "nd", "null")
    s.expect_listing(b, True)
    s.expect_get(b, "nd")
    s.put(b, "nd", body(r, 1, 20))
    s.versioning(b, "Enabled")
    s.put(b, "nd", body(r, 1, 20))
    s.versioning(b, "Suspended")
    s.delete_version(b, "nd", "null")       # a null version below the current one
    s.expect_listing(b, True)
    s.put(b, "nd", body(r, 1, 20))
    s.expect_listing(b, True)
    s.expect_get(b, "nd")
    s.expect_get(b, "nd", "null")


def position_rebuild_restart(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "rebuild-b"
    s.create(b, "Enabled")
    for cycle in range(3):
        for k in ("pr-b", "pr-a"):
            for _ in range(3):
                s.put(b, k, body(r, 1, 16))
        # The two newest versions of pr-a are the newest writes of all; they go away.
        newest = [v.id for v in s.m[b].history("pr-a")[:2]]
        for vid in newest:
            s.delete_version(b, "pr-a", vid)
        ctx.service.restart()
        s.expect_listing(b, True)
        s.put(b, "pr-a", body(r, 1, 16))     # must be newest, with a never-sent ID
        s.put(b, "pr-b", body(r, 1, 16))
        s.expect_listing(b, True)
        s.expect_get(b, "pr-a")


def ids_after_restart(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "restart-ids"
    s.create(b, "Enabled")
    for cycle in range(3):
        for _ in range(3):
            s.put(b, "ra", body(r, 1, 16))
        for _ in range(2):
            s.put(b, "rb", body(r, 1, 16))
        s.delete_version(b, "rb", s.m[b].history("rb")[0].id)
        ctx.service.restart()
        s.expect_listing(b, True)
        s.put(b, "rb", body(r, 1, 16))
        s.put(b, "ra", body(r, 1, 16))
        s.expect_listing(b, True)
