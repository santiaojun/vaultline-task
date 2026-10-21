"""The verifier's model of §3.4 histories and the §6 listing sequence.

Version IDs are never invented by the model: they come from the service's responses.
Positions are the model's own commit counter, used only to denote listing places.
"""

from __future__ import annotations

import copy
import hashlib


def etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


def ukey(s: str) -> bytes:
    return s.encode("utf-8")


class Version:
    __slots__ = ("id", "marker", "etag", "size", "ctype", "body", "pos")

    def __init__(self, vid, marker, body, ctype, pos):
        self.id = vid
        self.marker = marker
        self.body = body
        self.etag = None if marker else etag(body)
        self.size = None if marker else len(body)
        self.ctype = ctype
        self.pos = pos

    def __repr__(self):
        return f"{'M' if self.marker else 'V'}({self.id}@{self.pos})"


class Bucket:
    def __init__(self, name):
        self.name = name
        self.status = "Unversioned"
        self.keys: dict[str, list[Version]] = {}  # newest first; only keys with versions

    # ---- writes (§3.4)

    def put(self, model, key, vid, body, ctype="application/octet-stream"):
        if self.status == "Enabled":
            assert vid != "null", "Enabled PUT returned null"
        else:
            assert vid == "null", f"{self.status} PUT returned {vid!r}"
        h = self.keys.setdefault(key, [])
        if vid == "null":
            h[:] = [v for v in h if v.id != "null"]
        h.insert(0, Version(vid, False, body, ctype, model.next_pos()))

    def delete_key(self, model, key, marker_id=None):
        """Applies a key-DELETE; returns the expected (versionId, deleteMarker) response."""
        h = self.keys.setdefault(key, [])
        if self.status == "Enabled":
            assert marker_id not in (None, "null")
            h.insert(0, Version(marker_id, True, None, None, model.next_pos()))
            result = (marker_id, True)
        else:
            h[:] = [v for v in h if v.id != "null"]
            if self.status == "Suspended":
                h.insert(0, Version("null", True, None, None, model.next_pos()))
                result = ("null", True)
            else:
                result = ("null", False)
        if not h:
            del self.keys[key]
        return result

    def delete_version(self, key, vid):
        """Applies a delete-by-ID; returns the expected response, or None for NoSuchVersion."""
        h = self.keys.get(key, [])
        for i, v in enumerate(h):
            if v.id == vid:
                del h[i]
                if not h:
                    del self.keys[key]
                return (vid, v.marker)
        return None

    # ---- reads

    def history(self, key):
        return self.keys.get(key, [])

    def current(self, key):
        h = self.keys.get(key)
        return h[0] if h else None

    def find(self, key, vid):
        for v in self.keys.get(key, []):
            if v.id == vid:
                return v
        return None

    def sorted_keys(self):
        return sorted(self.keys, key=ukey)

    def sequence(self, versions, prefix="", delimiter="", start_after=None):
        """The §6.2 listing sequence as harness item tuples, each with its model place."""
        out = []
        seen = set()
        for k in self.sorted_keys():
            if not k.startswith(prefix):
                continue
            if start_after is not None and ukey(k) <= ukey(start_after):
                continue
            h = self.keys[k]
            if not versions and h[0].marker:
                continue
            rest = k[len(prefix):]
            if delimiter and delimiter in rest:
                cp = prefix + rest[:rest.index(delimiter) + len(delimiter)]
                if cp not in seen:
                    seen.add(cp)
                    out.append((("cp", cp), ("cp", cp)))
                continue
            if versions:
                for i, v in enumerate(h):
                    out.append((("v", k, v.id, i == 0, v.marker, v.etag, v.size), ("kpos", k, v.pos)))
            else:
                v = h[0]
                out.append((("o", k, v.id, v.etag, v.size), ("key", k)))
        return out

    def items_after(self, versions, place, prefix="", delimiter="", start_after=None):
        """Items of the current sequence that follow `place` (§6.4), with their places."""
        seq = self.sequence(versions, prefix, delimiter, None if place else start_after)
        if place is None:
            return seq
        kind = place[0]
        out = []
        for item, pl in seq:
            k = item[1]
            if kind == "cp":
                if ukey(k) > ukey(place[1]) and not k.startswith(place[1]):
                    out.append((item, pl))
            elif kind == "key":
                if ukey(k) > ukey(place[1]):
                    out.append((item, pl))
            else:  # kpos
                if ukey(k) > ukey(place[1]) or (k == place[1] and pl[0] == "kpos" and pl[2] < place[2]):
                    out.append((item, pl))
        return out

    def place_of(self, item):
        """The model place of an item the service returned (in the current state)."""
        if item[0] == "cp":
            return ("cp", item[1])
        if item[0] == "o":
            return ("key", item[1])
        v = self.find(item[1], item[2])
        assert v is not None, f"listed version {item[1]!r}/{item[2]} is not in the model"
        return ("kpos", item[1], v.pos)


class Model:
    def __init__(self):
        self.buckets: dict[str, Bucket] = {}
        self.pos = 0

    def next_pos(self):
        self.pos += 1
        return self.pos

    def create(self, name):
        assert name not in self.buckets
        self.buckets[name] = Bucket(name)
        return self.buckets[name]

    def delete(self, name):
        b = self.buckets[name]
        assert not b.keys, "BucketNotEmpty"
        del self.buckets[name]

    def __getitem__(self, name) -> Bucket:
        return self.buckets[name]

    def clone(self):
        return copy.deepcopy(self)


# ---------------------------------------------------------------- self-check


def _ids(b, key):
    return [v.id for v in b.history(key)]


def self_check():
    """Asserts that the model reproduces the spec's own examples exactly."""
    # §3.3: the ETag of an empty body.
    assert etag(b"") == '"d41d8cd98f00b204e9800998ecf8427e"'

    # §3.4: in a Suspended bucket, [v7, null, v2] becomes [null, v7, v2] after a PUT;
    # the new null version is newest and the old one is gone.
    m = Model()
    b = m.create("ex")
    b.status = "Enabled"
    b.put(m, "k", "v2", b"2")
    b.status = "Suspended"
    b.put(m, "k", "null", b"old")
    b.status = "Enabled"
    b.put(m, "k", "v7", b"7")
    assert _ids(b, "k") == ["v7", "null", "v2"]
    b.status = "Suspended"
    b.put(m, "k", "null", b"new")
    assert _ids(b, "k") == ["null", "v7", "v2"]
    assert b.current("k").body == b"new" and len([v for v in b.history("k") if v.id == "null"]) == 1

    # §3.4: after Unversioned -> Enabled an existing null version stays in place and the
    # next PUT adds a non-null version above it.
    b = m.create("ex2")
    b.put(m, "k", "null", b"u")
    b.status = "Enabled"
    b.put(m, "k", "vA", b"a")
    assert _ids(b, "k") == ["vA", "null"]

    # §3.4: an Unversioned bucket has at most one version per key and never a marker.
    b = m.create("ex3")
    b.put(m, "k", "null", b"1")
    b.put(m, "k", "null", b"2")
    assert _ids(b, "k") == ["null"] and b.current("k").body == b"2"

    # §5.5: the DELETE response table.
    b = m.create("del")
    assert b.delete_key(m, "absent") == ("null", False)              # Unversioned, no version
    b.put(m, "k", "null", b"x")
    assert b.delete_key(m, "k") == ("null", False) and "k" not in b.keys
    b.status = "Enabled"
    b.put(m, "k", "v1", b"x")
    assert b.delete_key(m, "k", "m1") == ("m1", True)
    assert b.delete_key(m, "k", "m2") == ("m2", True)                # marker on a marker
    assert _ids(b, "k") == ["m2", "m1", "v1"]
    b.status = "Suspended"
    assert b.delete_key(m, "fresh") == ("null", True)                # even with no versions
    assert b.delete_version("k", "m1") == ("m1", True)
    assert b.delete_version("k", "v1") == ("v1", False)
    assert b.delete_version("k", "v1") is None                       # retry -> NoSuchVersion
    assert b.current("k").id == "m2" and b.current("k").marker

    # §3.2 key order: UTF-8 bytes, and a prefix sorts first.
    b = m.create("order")
    b.status = "Enabled"
    for i, k in enumerate(["\U0001F600", "", "a/b", "a", "B"]):
        b.put(m, k, f"o{i}", b"")
    assert b.sorted_keys() == ["B", "a", "a/b", "", "\U0001F600"]

    # §6.2 grouping: delimiter occurrences inside the prefix are not considered.
    b = m.create("grp")
    b.status = "Enabled"
    for i, k in enumerate(["x/y/z", "x/w", "x/y/q"]):
        b.put(m, k, f"g{i}", b"g")
    items = [it for it, _ in b.sequence(False, prefix="x/", delimiter="/")]
    assert items == [("o", "x/w", "g1", etag(b"g"), 1), ("cp", "x/y/")]
    # §6.2: with startAfter, keys are filtered first and grouped afterwards.
    b.put(m, "p/1", "s1", b"")
    b.put(m, "p/2", "s2", b"")
    items = [it for it, _ in b.sequence(False, delimiter="/", start_after="p/1")]
    assert ("cp", "p/") in items and ("cp", "x/") in items
    # §6.2: in the objects listing a common prefix needs a key with a current object.
    b.delete_key(m, "x/w", "d1")
    b.delete_key(m, "x/y/z", "d2")
    b.delete_key(m, "x/y/q", "d3")
    assert ("cp", "x/") not in [it for it, _ in b.sequence(False, delimiter="/")]
    assert ("cp", "x/") in [it for it, _ in b.sequence(True, delimiter="/")]

    # §6.3 entry examples: object entry, then a version entry and a marker entry.
    b = m.create("shape")
    b.status = "Enabled"
    b.put(m, "k", "Q1", b"a")
    assert [it for it, _ in b.sequence(False)] == [("o", "k", "Q1", '"0cc175b9c0f1b6a831c399e269772661"', 1)]
    b.delete_key(m, "k", "Q2")
    assert [it for it, _ in b.sequence(True)] == [
        ("v", "k", "Q2", True, True, None, None),
        ("v", "k", "Q1", False, False, '"0cc175b9c0f1b6a831c399e269772661"', 1),
    ]
    assert b.sequence(False) == []

    # §6.4: a version-listing place is a history position, not an ID.
    b = m.create("place")
    b.status = "Enabled"
    b.put(m, "k", "A", b"")
    b.status = "Suspended"
    b.put(m, "k", "null", b"n")
    b.status = "Enabled"
    b.put(m, "k", "C", b"")          # history [C, null, A]
    place = b.place_of(("v", "k", "null"))
    b.status = "Suspended"
    b.put(m, "k", "null", b"n2")     # history [null', C, A]; the place is the removed null
    after = [it[2] for it, _ in b.items_after(True, place)]
    assert after == ["A"], after
