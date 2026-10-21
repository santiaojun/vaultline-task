"""Helpers shared by the checks: service and model in lockstep, exact listing comparison.

Cascade rule (design/scoring-budget.md): these helpers judge only status codes, bodies,
the ETag and Content-Length headers, and the JSON fields of write responses and
listings. Error codes, X-Vaultline-* headers, the JSON media type and the formats of
IDs and lastModified are asserted only in their own conformance checks.
"""

from __future__ import annotations

import random
import re

import model as M
from harness import expect, q

LM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")


def check_id(vid):
    expect(isinstance(vid, str) and (vid == "null" or ID_RE.match(vid)), f"version ID {vid!r} violates §3.3")


class Sync:
    """Runs each write on the service and on the model, and checks the response."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.c = ctx.client
        self.m = M.Model()
        self.sent = {}       # (bucket, lifetime, key) -> IDs the service sent for that key
        self.lifetime = {}
        self.lm = {}         # (bucket, key, model pos) -> lastModified

    # ---- buckets

    def create(self, b, versioning=None):
        r = self.c.create_bucket(b)
        expect(r.status == 201 and r.json() == {"name": b}, f"create {b}: {r!r}")
        self.m.create(b)
        self.lifetime[b] = self.lifetime.get(b, 0) + 1
        if versioning:
            self.versioning(b, versioning)
        return self.m[b]

    def delete_bucket(self, b):
        r = self.c.delete_bucket(b)
        expect(r.status == 200 and r.json() == {"name": b}, f"delete bucket {b}: {r!r}")
        self.m.delete(b)

    def versioning(self, b, status):
        r = self.c.set_versioning(b, status)
        expect(r.json().get("status") == status, f"versioning PUT {status}: {r.json()!r}")
        self.m[b].status = status

    # ---- IDs (§3.3)

    def _sent(self, b, key):
        return self.sent.setdefault((b, self.lifetime[b], key), set())

    def saw_id(self, b, key, vid):
        if vid != "null":
            self._sent(b, key).add(vid)

    def new_id(self, b, key, vid):
        expect(vid != "null", f"Enabled write on {key!r} returned null")
        expect(vid not in self._sent(b, key), f"ID {vid} was already sent for key {key!r} (§3.3)")
        self._sent(b, key).add(vid)

    # ---- objects

    def put(self, b, key, body, ctype=None):
        bucket = self.m[b]
        doc = self.c.put_ok(b, key, body, ctype)
        vid = doc["versionId"]
        if bucket.status == "Enabled":
            self.new_id(b, key, vid)
        else:
            expect(vid == "null", f"{bucket.status} PUT returned {vid!r}")
        expect(doc["key"] == key and doc["etag"] == M.etag(body) and doc["size"] == len(body),
               f"PUT response fields: {doc!r}")
        bucket.put(self.m, key, vid, body, ctype or "application/octet-stream")
        self.lm[(b, key, bucket.current(key).pos)] = doc["lastModified"]
        return vid

    def delete_key(self, b, key):
        bucket = self.m[b]
        doc = self.c.delete_ok(b, key)
        if bucket.status == "Enabled":
            expect(doc["deleteMarker"] is True, f"Enabled key-DELETE: {doc!r}")
            self.new_id(b, key, doc["versionId"])
            want = bucket.delete_key(self.m, key, doc["versionId"])
        else:
            want = bucket.delete_key(self.m, key)
        expect((doc["versionId"], doc["deleteMarker"]) == want, f"key-DELETE {key!r}: got {doc!r}, want {want}")
        return doc["versionId"]

    def delete_version(self, b, key, vid):
        bucket = self.m[b]
        want = bucket.delete_version(key, vid)
        r = self.c.delete(b, key, vid)
        if want is None:
            expect(r.status == 404, f"delete missing {vid}: {r!r}")
            return None
        expect(r.status == 200, f"delete {key!r} {vid}: {r!r}")
        doc = r.json()
        expect((doc.get("versionId"), doc.get("deleteMarker")) == want, f"delete-by-ID {vid}: {doc!r}, want {want}")
        return want

    # ---- comparisons

    def expect_get(self, b, key, version=None):
        """GET (and HEAD) answer as the model says (§5.4)."""
        bucket = self.m[b]
        v = bucket.current(key) if version is None else bucket.find(key, version)
        for method in ("GET", "HEAD"):
            r = self.c.get(b, key, version, method=method)
            what = f"{method} {b}/{key!r} versionId={version!r}"
            if v is None:
                expect(r.status == 404, f"{what}: want 404, got {r!r}")
            elif v.marker:
                want = 404 if version is None else 405
                expect(r.status == want, f"{what}: marker, want {want}, got {r!r}")
            else:
                expect(r.status == 200, f"{what}: want 200, got {r!r}")
                expect(r.headers.get("etag") == v.etag, f"{what}: ETag {r.headers.get('etag')!r}, want {v.etag}")
                expect(r.headers.get("content-length") == str(v.size), f"{what}: Content-Length")
                if method == "GET":
                    expect(r.body == v.body, f"{what}: body differs")
        return v

    def expect_listing(self, b, versions, prefix=None, delimiter=None, max_keys=None, start_after=None):
        """A complete quiet iteration equals the model's sequence, page by page (§6.3, §6.4)."""
        bucket = self.m[b]
        seq = [it for it, _ in bucket.sequence(versions, prefix or "", delimiter or "", start_after)]
        items, pages = self.c.list_all(b, versions, prefix, delimiter, max_keys, start_after)
        expect(items == seq, f"{'versions' if versions else 'objects'} listing of {b} "
                             f"(prefix={prefix!r} delimiter={delimiter!r} maxKeys={max_keys} "
                             f"startAfter={start_after!r}) differs from the model:\n got  {items[:12]}\n want {seq[:12]}")
        mk = max_keys or 1000
        for p in pages[:-1]:
            expect(len(p["items"]) == mk, f"truncated page holds {len(p['items'])} items, maxKeys {mk}")
        return items

    def paginate(self, b, versions, max_keys, prefix=None, delimiter=None, between=None, start_after=None):
        """Pages one at a time; each page must equal the model's items after the previous
        page's place in the state at request time. `between(i)` runs writes between pages."""
        bucket = self.m[b]
        place, token, i, got = None, None, 0, []
        while True:
            want_all = bucket.items_after(versions, place, prefix or "", delimiter or "", start_after)
            want = [it for it, _ in want_all[:max_keys]]
            page = self.c.list_page(b, versions, prefix, delimiter, max_keys, token,
                                    start_after if token is None else None)
            expect(page["items"] == want, f"page {i} of {b} differs from the model:\n got  {page['items']}\n want {want}")
            expect(page["isTruncated"] == (len(want_all) > max_keys), f"page {i}: isTruncated {page['isTruncated']}")
            got.append(page["items"])
            if not page["isTruncated"]:
                return got
            place = want_all[max_keys - 1][1]
            token = page["nextToken"]
            if between:
                between(i)
            i += 1


def rnd(ctx):
    return random.Random(ctx.seed)


def body(r, lo=0, hi=64):
    return bytes(r.getrandbits(8) for _ in range(r.randint(lo, hi)))


__all__ = ["Sync", "rnd", "body", "check_id", "LM_RE", "expect", "q", "M"]
