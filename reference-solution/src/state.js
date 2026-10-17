'use strict';
// In-memory state. `apply` is the only function that mutates it; it is used
// both for live writes (after the record is in the log) and for replay.
// The plan* functions are pure: they validate against the current state and
// return either { error } or { record }.

function createState() {
  return { seq: 0, buckets: new Map() };
}

function newBucket(name) {
  // order: entries sorted by UTF-8 bytes of the key; only keys with versions.
  return { name, status: 'Unversioned', keys: new Map(), order: [] };
}

function versionId(seq) {
  return 'v' + seq.toString(36);
}

function findIndex(order, buf) {
  let lo = 0;
  let hi = order.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (Buffer.compare(order[mid].buf, buf) < 0) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function getOrCreateEntry(bucket, key) {
  let entry = bucket.keys.get(key);
  if (!entry) {
    entry = { key, buf: Buffer.from(key, 'utf8'), history: [] };
    bucket.keys.set(key, entry);
    bucket.order.splice(findIndex(bucket.order, entry.buf), 0, entry);
  }
  return entry;
}

function dropEntryIfEmpty(bucket, entry) {
  if (entry.history.length > 0) return;
  bucket.keys.delete(entry.key);
  const i = findIndex(bucket.order, entry.buf);
  bucket.order.splice(i, 1);
}

function removeById(entry, id) {
  const i = entry.history.findIndex((v) => v.id === id);
  if (i >= 0) entry.history.splice(i, 1);
}

function apply(state, r) {
  const bucket = state.buckets.get(r.bucket);
  switch (r.t) {
    case 'createBucket':
      state.buckets.set(r.bucket, newBucket(r.bucket));
      break;
    case 'deleteBucket':
      state.buckets.delete(r.bucket);
      break;
    case 'setVersioning':
      bucket.status = r.status;
      break;
    case 'putObject': {
      const entry = getOrCreateEntry(bucket, r.key);
      if (r.id === 'null') removeById(entry, 'null');
      entry.history.unshift({
        pos: r.seq, id: r.id, marker: false, etag: r.etag, size: r.size,
        ctype: r.ctype, lm: r.lm, body: r.body,
      });
      break;
    }
    case 'deleteKey': {
      const entry = getOrCreateEntry(bucket, r.key);
      if (r.removeNull) removeById(entry, 'null');
      if (r.marker !== null) {
        entry.history.unshift({
          pos: r.seq, id: r.marker, marker: true, etag: null, size: null,
          ctype: null, lm: r.lm, body: null,
        });
      }
      dropEntryIfEmpty(bucket, entry);
      break;
    }
    case 'deleteVersion': {
      const entry = bucket.keys.get(r.key);
      removeById(entry, r.id);
      dropEntryIfEmpty(bucket, entry);
      break;
    }
    default:
      throw new Error('unknown record type ' + r.t);
  }
  state.seq = r.seq;
}

function nextLm(entry, now) {
  const newest = entry && entry.history[0];
  return newest && newest.lm > now ? newest.lm : now;
}

function planCreateBucket(state, name) {
  if (state.buckets.has(name)) return { error: 'BucketAlreadyExists' };
  return { record: { t: 'createBucket', seq: state.seq + 1, bucket: name } };
}

function planDeleteBucket(state, name) {
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  if (bucket.keys.size > 0) return { error: 'BucketNotEmpty' };
  return { record: { t: 'deleteBucket', seq: state.seq + 1, bucket: name } };
}

// record is null when the state is already `status` (no change to log).
function planSetVersioning(state, name, status) {
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  if (bucket.status === status) return { record: null };
  return { record: { t: 'setVersioning', seq: state.seq + 1, bucket: name, status } };
}

function planPutObject(state, name, key, obj, now) {
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  const seq = state.seq + 1;
  return {
    record: {
      t: 'putObject', seq, bucket: name, key,
      id: bucket.status === 'Enabled' ? versionId(seq) : 'null',
      etag: obj.etag, size: obj.size, ctype: obj.ctype,
      lm: nextLm(bucket.keys.get(key), now), body: obj.body,
    },
  };
}

// record is null when nothing changes (Unversioned key without a null version).
function planDeleteKey(state, name, key, now) {
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  const seq = state.seq + 1;
  const entry = bucket.keys.get(key);
  const hasNull = !!entry && entry.history.some((v) => v.id === 'null');
  let removeNull = false;
  let marker = null;
  if (bucket.status === 'Enabled') {
    marker = versionId(seq);
  } else if (bucket.status === 'Suspended') {
    removeNull = hasNull;
    marker = 'null';
  } else {
    removeNull = hasNull;
  }
  if (!removeNull && marker === null) return { record: null };
  return {
    record: {
      t: 'deleteKey', seq, bucket: name, key, removeNull, marker,
      lm: nextLm(entry, now),
    },
  };
}

function planDeleteVersion(state, name, key, id) {
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  const entry = bucket.keys.get(key);
  const version = entry && entry.history.find((v) => v.id === id);
  if (!version) return { error: 'NoSuchVersion' };
  return {
    record: { t: 'deleteVersion', seq: state.seq + 1, bucket: name, key, id },
    version,
  };
}

module.exports = {
  createState, apply, versionId,
  planCreateBucket, planDeleteBucket, planSetVersioning,
  planPutObject, planDeleteKey, planDeleteVersion,
};
