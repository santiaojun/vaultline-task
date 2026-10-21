"""F8 Conformance (§2, §4, §9): error envelope, code/status pairs, precedence, header names."""

from common import expect, q


def _error(r, status, code, what):
    expect(r.status == status, f"{what}: status {r.status}, want {status}")
    expect(r.json_media_type(), f"{what}: Content-Type {r.headers.get('content-type')!r} is not application/json")
    doc = r.json()
    expect(isinstance(doc, dict) and isinstance(doc.get("error"), dict)
           and doc["error"].get("code") == code and isinstance(doc["error"].get("message"), str),
           f"{what}: error body {doc!r}, want code {code}")


def errors_and_headers(ctx):
    c = ctx.client
    h = c.request("GET", "/health")
    expect(h.status == 200, "/health")
    r = c.create_bucket("conform-b")
    expect(r.status == 201 and r.json_media_type(), "JSON media type on 201")
    c.set_versioning("conform-b", "Enabled")
    put = c.put_ok("conform-b", "item", b"conformance body")
    expect(c.request("GET", "/buckets").json_media_type(), "JSON media type on GET /buckets")
    mid = c.delete_ok("conform-b", "gone")["versionId"]

    _error(c.create_bucket("BAD"), 400, "InvalidBucketName", "invalid bucket name")
    _error(c.request("GET", "/buckets/conform-b/object?key="), 400, "InvalidKey", "empty key")
    _error(c.request("GET", "/buckets/conform-b/objects?maxKeys=0"), 400, "InvalidArgument", "maxKeys")
    _error(c.request("GET", "/buckets/conform-b/objects?token="), 400, "InvalidToken", "empty token")
    _error(c.request("GET", "/buckets/nope-b/versioning"), 404, "NoSuchBucket", "missing bucket")
    _error(c.get("conform-b", "absent"), 404, "NoSuchKey", "missing key")
    _error(c.get("conform-b", "item", "zz-no-such"), 404, "NoSuchVersion", "missing version")
    _error(c.get("conform-b", "gone", mid), 405, "MethodNotAllowed", "marker by ID")
    _error(c.create_bucket("conform-b"), 409, "BucketAlreadyExists", "duplicate bucket")
    _error(c.delete_bucket("conform-b"), 409, "BucketNotEmpty", "non-empty bucket")
    hd = c.get("conform-b", "absent", method="HEAD")
    expect(hd.status == 404 and hd.body == b"", "HEAD error response has no body")

    # Precedence (§9): state-independent 400 errors win over NoSuchBucket; NoSuchBucket
    # wins over NoSuchKey/NoSuchVersion and over 409 errors.
    _error(c.request("GET", "/buckets/nope-b/object?key="), 400, "InvalidKey", "precedence InvalidKey")
    _error(c.request("GET", "/buckets/nope-b/objects?maxKeys=1001"), 400, "InvalidArgument", "precedence maxKeys")
    _error(c.request("GET", "/buckets/nope-b/versions?token="), 400, "InvalidToken", "precedence token")
    r = c.request("GET", "/buckets/nope-b/objects?startAfter=a&token=")
    expect(r.status == 400, f"precedence: token with startAfter on a missing bucket: {r.status}")
    _error(c.request("GET", f"/buckets/nope-b/object?key={q('k2')}"), 404, "NoSuchBucket", "NoSuchBucket over NoSuchKey")
    _error(c.request("DELETE", f"/buckets/nope-b/object?key={q('k2')}&versionId=v"), 404, "NoSuchBucket",
           "NoSuchBucket over NoSuchVersion")
    _error(c.delete_bucket("nope-b"), 404, "NoSuchBucket", "delete missing bucket")

    # Header names (§5.4).
    g = c.get("conform-b", "item")
    expect(g.headers.get("x-vaultline-version-id") == put["versionId"]
           and g.headers.get("x-vaultline-last-modified") == put["lastModified"]
           and "x-vaultline-delete-marker" not in g.headers, f"object headers {g.headers}")
    m = c.get("conform-b", "gone")
    expect(m.headers.get("x-vaultline-delete-marker") == "true" and m.headers.get("x-vaultline-version-id") == mid
           and "x-vaultline-last-modified" in m.headers and "etag" not in m.headers, f"marker headers {m.headers}")
