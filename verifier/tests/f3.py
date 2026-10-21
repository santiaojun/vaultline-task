"""F3 Markers & current version (§3.4, §5.4, §5.5, §6.2).

No restarts. `delimiter` is used only in long_token, which has no markers.
Markers on marker-only keys are deleted oldest-first.
"""

from common import Sync, body, expect, q, rnd


def marker_responses(ctx):
    s = Sync(ctx)
    c = s.c
    r = rnd(ctx)
    b = "mresp-b"
    s.create(b, "Enabled")
    s.put(b, "doc", body(r, 1, 20))
    mid = s.delete_key(b, "doc")
    for method in ("GET", "HEAD"):
        g = c.get(b, "doc", method=method)
        expect(g.status == 404, f"{method} on marker-current key: {g!r}")
        expect(g.headers.get("x-vaultline-delete-marker") == "true" and g.headers.get("x-vaultline-version-id") == mid
               and "x-vaultline-last-modified" in g.headers and "etag" not in g.headers, f"{method} marker headers")
        g = c.get(b, "doc", mid, method=method)
        expect(g.status == 405, f"{method} marker by ID: {g!r}")
        expect(g.headers.get("x-vaultline-delete-marker") == "true" and g.headers.get("x-vaultline-version-id") == mid
               and "etag" not in g.headers, f"{method} 405 marker headers")
    expect(c.get(b, "doc").error_code() == "NoSuchKey", "404 code")
    expect(c.get(b, "doc", mid).error_code() == "MethodNotAllowed", "405 code")
    # §5.5 bodies in the three states.
    s.versioning(b, "Suspended")
    expect(c.delete_ok(b, "nothing-here") == {"versionId": "null", "deleteMarker": True}, "Suspended DELETE body")
    s.m[b].delete_key(s.m, "nothing-here")
    s.create("mresp-u")
    expect(c.delete_ok("mresp-u", "nothing") == {"versionId": "null", "deleteMarker": False}, "Unversioned DELETE")
    s.put("mresp-u", "there", body(r, 1, 5))
    expect(c.delete_ok("mresp-u", "there") == {"versionId": "null", "deleteMarker": False}, "Unversioned DELETE")
    s.m["mresp-u"].delete_key(s.m, "there")
    expect(c.get("mresp-u", "there").status == 404, "Unversioned DELETE removed the version")
    expect(s.delete_version(b, "doc", mid) == (mid, True), "delete marker by ID")
    r2 = c.delete(b, "doc", mid)
    expect(r2.status == 404 and r2.error_code() == "NoSuchVersion", f"retried delete-by-ID: {r2!r}")


def marker_hides(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "hide-b"
    s.create(b, "Enabled")
    for k in ("h1", "h2", "h3"):
        s.put(b, k, body(r, 1, 20))
    s.delete_key(b, "h2")
    g = s.c.get(b, "h2")
    expect(g.status == 404, f"GET hidden key: {g!r}")
    items = s.expect_listing(b, False)
    expect([it[1] for it in items] == ["h1", "h3"], f"objects listing {items}")


def older_readable(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "older-b"
    s.create(b, "Enabled")
    ids = [s.put(b, "kept", body(r, 5, 40)) for _ in range(3)]
    s.delete_key(b, "kept")
    for vid in ids:
        s.expect_get(b, "kept", vid)
    s.expect_listing(b, True)


def marker_stacking(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "stack-b"
    s.create(b, "Enabled")
    s.put(b, "st", body(r, 1, 10))
    markers = [s.delete_key(b, "st") for _ in range(4)]     # Sync.new_id: each one new
    expect(len(set(markers)) == 4, f"markers {markers}")
    s.delete_key(b, "never-written")
    s.delete_key(b, "never-written")
    s.expect_listing(b, True)
    s.expect_get(b, "st")


def marker_sequence(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "mseq-b"
    s.create(b, "Enabled")
    keys = [f"q{i}" for i in range(6)]
    patterns = {"stacked": False, "over_marker": False}
    for step in range(300):
        x = r.random()
        k = r.choice(keys)
        bucket = s.m[b]
        h = bucket.history(k)
        if x < 0.45:
            s.put(b, k, body(r, 0, 24))
        elif x < 0.75:
            patterns["stacked"] |= bool(h) and h[0].marker
            s.delete_key(b, k)
        elif h:
            v = r.choice(h)
            patterns["over_marker"] |= v is h[0] and len(h) > 1 and h[1].marker
            s.delete_version(b, k, v.id)
        if step % 50 == 49:
            s.expect_listing(b, True)
            s.expect_listing(b, False)
            for key in keys:
                s.expect_get(b, key)
    expect(all(patterns.values()), f"VERIFIER: seeded sequence lacks a pattern {patterns}")


def recompute_next_marker(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "recomp-b"
    s.create(b, "Enabled")
    s.put(b, "rc", body(r, 1, 20))
    s.delete_key(b, "rc")
    top = s.put(b, "rc", body(r, 1, 20))
    s.put(b, "rc-other", body(r, 1, 20))
    s.delete_version(b, "rc", top)          # next-newest is a marker
    g = s.expect_get(b, "rc")
    expect(g.marker, "model: marker is current")
    items = s.expect_listing(b, False)
    expect("rc" not in [it[1] for it in items], "key with a current marker in the objects listing")
    vitems = s.expect_listing(b, True)
    latest = [it for it in vitems if it[1] == "rc" and it[3]]
    expect(len(latest) == 1 and latest[0][4] is True, f"isLatest should be on the marker: {latest}")


def null_marker_delete_by_id(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "nmark-b"
    s.create(b, "Enabled")
    s.put(b, "nm", body(r, 1, 20))
    s.delete_key(b, "nm")
    s.versioning(b, "Suspended")
    s.delete_key(b, "nm")                   # [null marker, marker, object]
    s.delete_version(b, "nm", "null")
    s.expect_get(b, "nm")                   # the non-null marker is current again
    v1 = s.m[b].history("nm")[-1].id
    s.expect_get(b, "nm", v1)
    s.expect_listing(b, True)
    s.expect_listing(b, False)


def current_after_delete_by_id(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "cur-b"
    s.create(b, "Enabled")
    for _ in range(4):
        s.put(b, "cu", body(r, 1, 20))
    while s.m[b].history("cu"):
        s.delete_version(b, "cu", s.m[b].history("cu")[0].id)
        s.expect_get(b, "cu")
        s.expect_listing(b, False)
    s.expect_listing(b, True)


def undelete(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "undel-b"
    s.create(b, "Enabled")
    for k in ("ua", "ub", "uc"):
        s.put(b, k, body(r, 1, 20))
        s.put(b, k, body(r, 1, 20))
    mid = s.delete_key(b, "ub")
    s.expect_listing(b, False)
    s.delete_version(b, "ub", mid)
    s.expect_get(b, "ub")
    items = s.expect_listing(b, False)
    expect("ub" in [it[1] for it in items], "undeleted key missing from the objects listing")


def marker_only_versions(ctx):
    s = Sync(ctx)
    b = "monly-b"
    s.create(b, "Enabled")
    s.put(b, "live", b"live body")
    for k in ("mo1", "mo2"):
        s.delete_key(b, k)
    s.delete_key(b, "mo3")
    for k in ("mo1", "mo2", "mo3"):
        s.expect_get(b, k)
    s.expect_listing(b, True)
    items = s.expect_listing(b, False)
    expect([it[1] for it in items] == ["live"], f"objects listing {items}")
    for k in ("mo1", "mo2", "mo3"):
        for v in list(s.m[b].history(k))[::-1]:     # oldest first
            s.delete_version(b, k, v.id)
    s.expect_listing(b, True)
    s.expect_listing(b, False)


def suspended_delete(ctx):
    s = Sync(ctx)
    r = rnd(ctx)
    b = "susdel-b"
    s.create(b, "Enabled")
    s.put(b, "sd", body(r, 1, 20))
    s.versioning(b, "Suspended")
    s.put(b, "sd", body(r, 1, 20))
    s.delete_key(b, "sd")                   # removes the null object, adds a null marker
    s.delete_key(b, "sd-none")
    s.expect_listing(b, True)
    s.expect_get(b, "sd")
    s.expect_get(b, "sd", "null")


def long_token(ctx):
    s = Sync(ctx)
    b = "longtok"
    s.create(b, "Enabled")
    prefix = "p" * 1000
    for last in "ab":
        s.put(b, prefix + "q" * 23 + last, last.encode())
    path_len = len(f"/buckets/{b}/versions?prefix=&delimiter=&maxKeys=1&token=")
    delimiter = "~" * (8192 - path_len - len(prefix))
    target = f"/buckets/{b}/versions?prefix={q(prefix)}&delimiter={q(delimiter)}&maxKeys=1&token="
    expect(len(target) == 8192, f"VERIFIER: target is {len(target)} bytes")
    s.paginate(b, True, 1, prefix=prefix, delimiter=delimiter)
