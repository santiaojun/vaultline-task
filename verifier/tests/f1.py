"""F1 Objects & keys (§3.2, §3.3, §5.3, §5.4)."""

from common import LM_RE, M, Sync, body, expect, rnd


def object_basics(ctx):
    s = Sync(ctx)
    c = s.c
    s.create("basics-one")
    r = rnd(ctx)
    data = body(r, 300, 600)
    doc = c.put_ok("basics-one", "doc.txt", data, "text/plain; charset=utf-8")
    expect(doc["versionId"] == "null" and doc["key"] == "doc.txt" and doc["size"] == len(data)
           and doc["etag"] == M.etag(data) and LM_RE.match(doc["lastModified"]), f"PUT response {doc!r}")
    g = c.get("basics-one", "doc.txt")
    expect(g.status == 200 and g.body == data, "GET body")
    h = g.headers
    expect(h.get("etag") == M.etag(data), f"ETag header {h.get('etag')!r}")
    expect(h.get("content-length") == str(len(data)), "Content-Length")
    expect(h.get("content-type") == "text/plain; charset=utf-8", f"Content-Type {h.get('content-type')!r}")
    expect(h.get("x-vaultline-version-id") == "null", "X-Vaultline-Version-Id")
    expect(h.get("x-vaultline-last-modified") == doc["lastModified"], "X-Vaultline-Last-Modified")
    expect("x-vaultline-delete-marker" not in h, "X-Vaultline-Delete-Marker on an object version")
    hd = c.get("basics-one", "doc.txt", method="HEAD")
    expect(hd.status == 200 and hd.body == b"", "HEAD has no body")
    for name in ("etag", "content-length", "content-type", "x-vaultline-version-id", "x-vaultline-last-modified"):
        expect(hd.headers.get(name) == h.get(name), f"HEAD header {name}: {hd.headers.get(name)!r}")
    # Absent Content-Type is stored as application/octet-stream.
    other = body(r, 10, 40)
    c.put_ok("basics-one", "plain", other)
    expect(c.get("basics-one", "plain").headers.get("content-type") == "application/octet-stream",
           "default Content-Type")
    # A body of length 0 is valid.
    e = c.put_ok("basics-one", "empty-one", b"", "application/x-empty-test")
    expect(e["size"] == 0 and e["etag"] == M.etag(b""), f"empty PUT {e!r}")
    g = c.get("basics-one", "empty-one")
    expect(g.status == 200 and g.body == b"" and g.headers.get("content-length") == "0", "empty GET")
    # Overwrite in an Unversioned bucket.
    newer = body(r, 50, 80)
    c.put_ok("basics-one", "doc.txt", newer, "text/markdown")
    g = c.get("basics-one", "doc.txt")
    expect(g.body == newer and g.headers.get("content-type") == "text/markdown", "overwrite")
    for k in ("missing-one",):
        g = c.get("basics-one", k)
        expect(g.status == 404 and g.error_code() == "NoSuchKey", f"missing key: {g!r}")


KEYS = ["m", "m/n", "m/", "/m", "//", ".", "..", "./m", "m/../n", "M", "x y", "x+y", "%41", "A",
        "back\\slash", "é", "é", "seg/" + "z" * 300 + "/end", "L" * 1024,
        "€" * 341 + "q", "m.", "m./"]


def key_mapping(ctx):
    s = Sync(ctx)
    s.create("keymap-b")
    r = rnd(ctx)
    for k in KEYS:
        assert len(k.encode()) <= 1024
        s.put("keymap-b", k, body(r, 1, 50) + k.encode())
    for k in KEYS:
        s.expect_get("keymap-b", k)
    s.expect_listing("keymap-b", False)
    s.expect_listing("keymap-b", True, max_keys=5)


BOM_KEYS = ["﻿start", "mid﻿dle", "￿", "x\U0001F600y", "\U0010FFFF", "﻿"]


def bom_key(ctx):
    s = Sync(ctx)
    s.create("bom-keys")
    r = rnd(ctx)
    for k in BOM_KEYS:
        s.put("bom-keys", k, body(r, 1, 30))
    for k in BOM_KEYS:
        s.expect_get("bom-keys", k)
    s.expect_listing("bom-keys", False)
    s.expect_listing("bom-keys", True, max_keys=2)
    s.expect_listing("bom-keys", False, prefix="﻿")
