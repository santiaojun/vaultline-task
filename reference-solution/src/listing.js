'use strict';
// Object and version listings (§6). A page is computed in one synchronous
// pass over the bucket's sorted keys, so it is a snapshot of committed state.
// Tokens are stateless: base64url(JSON) of the binding and the place after
// the page's last item.
const { findIndex } = require('./state');

function iso(ms) {
  return new Date(ms).toISOString();
}

function encodeToken(t) {
  return Buffer.from(JSON.stringify(t), 'utf8').toString('base64url');
}

// Returns the token's place, or null if the token is not one this service
// issued for this endpoint, bucket, prefix and delimiter.
function decodeToken(s, binding) {
  if (s === '') return null;
  const buf = Buffer.from(s, 'base64url');
  if (buf.toString('base64url') !== s) return null;
  let t;
  try {
    t = JSON.parse(buf.toString('utf8'));
  } catch {
    return null;
  }
  if (t === null || typeof t !== 'object') return null;
  if (t.e !== binding.e || t.b !== binding.b || t.p !== binding.p || t.d !== binding.d) return null;
  if (typeof t.c === 'string') return { c: t.c };
  if (typeof t.k !== 'string') return null;
  if (binding.e === 'v') {
    if (!Number.isSafeInteger(t.pos)) return null;
    return { k: t.k, pos: t.pos };
  }
  return { k: t.k };
}

// First index in `order` whose key is greater than `key`.
function upperBound(order, key) {
  const buf = Buffer.from(key, 'utf8');
  let i = findIndex(order, buf);
  if (i < order.length && order[i].key === key) i++;
  return i;
}

function skipUnder(order, i, cp) {
  while (i < order.length && order[i].key.startsWith(cp)) i++;
  return i;
}

// opts: { versions, prefix, delimiter, maxKeys, startAfter, place }
function listPage(bucket, opts) {
  const { versions, prefix, delimiter, maxKeys, place } = opts;
  const order = bucket.order;

  let i = findIndex(order, Buffer.from(prefix, 'utf8'));
  let startPos = null;
  if (place) {
    if (place.c !== undefined) {
      i = Math.max(i, skipUnder(order, findIndex(order, Buffer.from(place.c, 'utf8')), place.c));
    } else if (place.pos !== undefined) {
      const j = findIndex(order, Buffer.from(place.k, 'utf8'));
      if (j < order.length && order[j].key === place.k) startPos = place.pos;
      i = Math.max(i, j);
    } else {
      i = Math.max(i, upperBound(order, place.k));
    }
  } else if (opts.startAfter !== undefined) {
    i = Math.max(i, upperBound(order, opts.startAfter));
  }

  // Collect up to maxKeys + 1 items; the extra one decides isTruncated.
  const items = [];
  const first = i;
  for (; i < order.length && items.length <= maxKeys; i++) {
    const entry = order[i];
    if (!entry.key.startsWith(prefix)) break;
    if (!versions && entry.history[0].marker) continue;
    if (delimiter !== '') {
      const rest = entry.key.slice(prefix.length);
      const at = rest.indexOf(delimiter);
      if (at >= 0) {
        const cp = prefix + rest.slice(0, at + delimiter.length);
        items.push({ cp });
        i = skipUnder(order, i, cp) - 1;
        continue;
      }
    }
    if (!versions) {
      items.push({ entry, v: entry.history[0] });
      continue;
    }
    for (const v of entry.history) {
      if (i === first && startPos !== null && v.pos >= startPos) continue;
      items.push({ entry, v });
      if (items.length > maxKeys) break;
    }
  }

  const isTruncated = items.length > maxKeys;
  const page = isTruncated ? items.slice(0, maxKeys) : items;
  const entries = [];
  const commonPrefixes = [];
  for (const it of page) {
    if (it.cp !== undefined) {
      commonPrefixes.push(it.cp);
    } else if (versions) {
      const v = it.v;
      entries.push({
        key: it.entry.key, versionId: v.id, isLatest: v === it.entry.history[0],
        deleteMarker: v.marker, etag: v.etag, size: v.size, lastModified: iso(v.lm),
      });
    } else {
      const v = it.v;
      entries.push({ key: it.entry.key, versionId: v.id, etag: v.etag, size: v.size, lastModified: iso(v.lm) });
    }
  }

  let nextToken = null;
  if (isTruncated) {
    const last = page[page.length - 1];
    const t = { e: versions ? 'v' : 'o', b: bucket.name, p: prefix, d: delimiter };
    if (last.cp !== undefined) t.c = last.cp;
    else {
      t.k = last.entry.key;
      if (versions) t.pos = last.v.pos;
    }
    nextToken = encodeToken(t);
  }
  return { entries, commonPrefixes, isTruncated, nextToken };
}

module.exports = { listPage, decodeToken, iso };
