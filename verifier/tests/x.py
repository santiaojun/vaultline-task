"""X-integration: one seeded sequence across features with restarts, compared with the model.

A shared integration check, outside every mutant's target set. The scripted opening
contains each pattern the semantic mutants depend on, so they fail it on every run.
"""

from common import Sync, body, expect, rnd
from harness import CheckFailed


def integration(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    svc = ctx.service
    s.create("x-a", "Enabled")
    s.create("x-b", "Enabled")
    s.create("x-c")

    # Delete the newest version by ID, restart, PUT (positions and IDs after restart).
    for _ in range(3):
        s.put("x-a", "x1", body(r, 1, 9))
    s.delete_version("x-a", "x1", s.m["x-a"].history("x1")[0].id)
    svc.restart()
    s.put("x-a", "x1", body(r, 1, 9))
    # Markers on a marker-current key.
    s.delete_key("x-a", "x2")
    s.delete_key("x-a", "x2")
    # Delete the current version by ID when a marker is next.
    s.put("x-a", "x3", body(r, 1, 9))
    s.delete_key("x-a", "x3")
    top = s.put("x-a", "x3", body(r, 1, 9))
    s.delete_version("x-a", "x3", top)
    s.expect_get("x-a", "x3")
    # Suspended PUT over a null version that is not the newest.
    s.put("x-b", "y1", body(r, 1, 9))
    s.versioning("x-b", "Suspended")
    s.put("x-b", "y1", body(r, 1, 9))
    s.versioning("x-b", "Enabled")
    s.put("x-b", "y1", body(r, 1, 9))
    s.versioning("x-b", "Suspended")
    s.put("x-b", "y1", body(r, 1, 9))
    s.expect_listing("x-b", True)
    # A delimiter listing over marker-only keys.
    s.delete_key("x-a", "dir/m1")
    s.put("x-a", "dir2/live", body(r, 1, 9))
    s.expect_listing("x-a", False, delimiter="/")
    # Writes in a key's history between pages: remove the version at the place.
    for _ in range(5):
        s.put("x-a", "x0", body(r, 1, 9))
    h = list(s.m["x-a"].history("x0"))
    s.paginate("x-a", True, 2, prefix="x0",          # page 1 = the two newest versions of x0
               between=lambda i: s.delete_version("x-a", "x0", h[1].id) if i == 0 else None)
    # A restart right after a key-DELETE is acknowledged.
    s.put("x-a", "x4", body(r, 1, 9))
    s.delete_key("x-a", "x4")
    svc.restart()
    s.expect_get("x-a", "x4")

    # Seeded mixed sequence.
    buckets = ["x-a", "x-b", "x-c"]
    keys = ["k/a", "k/b", "k/c/d", "m", "m/n", "q"]
    for step in range(150):
        b = r.choice(buckets)
        bucket = s.m[b]
        k = r.choice(keys)
        x = r.random()
        if x < 0.06 and b != "x-c":
            s.versioning(b, r.choice(["Enabled", "Suspended"]))
        elif x < 0.5:
            s.put(b, k, body(r, 0, 20))
        elif x < 0.72:
            s.delete_key(b, k)
        elif bucket.history(k):
            s.delete_version(b, k, r.choice(bucket.history(k)).id)
        if step % 30 == 29:
            lb = r.choice(buckets)

            def between(i, lb=lb):
                if i >= 3:
                    return
                bk = s.m[lb]
                kk = r.choice(keys)
                if bk.history(kk) and r.random() < 0.5:
                    s.delete_version(lb, kk, r.choice(bk.history(kk)).id)
                else:
                    s.put(lb, kk, body(r, 0, 9))
            s.paginate(lb, r.random() < 0.6, r.choice([1, 2, 3]), delimiter=r.choice([None, "/"]), between=between)
        if step % 50 == 49:
            svc.restart()
            for b2 in buckets:
                s.expect_listing(b2, True)
                s.expect_listing(b2, False, delimiter="/")
                for k2 in keys:
                    s.expect_get(b2, k2)
    if svc.writes > 300:
        raise CheckFailed(f"VERIFIER: {svc.writes} writes exceed the 300-record restart rule")
