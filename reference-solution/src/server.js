'use strict';
// HTTP routing. Every write is committed synchronously in commit(): plan
// against memory, append the record, apply it. Nothing yields in between, so
// writes are serialized by the event loop, and memory (from which every
// response is built) changes only after the record has reached the OS.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { pipeline } = require('node:stream/promises');
const { recover } = require('./recovery');
const S = require('./state');
const { parseTarget, decodeUtf8, validBucketName, decodeKey } = require('./query');
const { listPage, decodeToken, iso } = require('./listing');

const MAX_BODY = 16777216;

const STATUS = {
  InvalidBucketName: 400, InvalidKey: 400, InvalidArgument: 400, InvalidToken: 400,
  EntityTooLarge: 413, NoSuchBucket: 404, NoSuchKey: 404, NoSuchVersion: 404,
  MethodNotAllowed: 405, BucketAlreadyExists: 409, BucketNotEmpty: 409, InternalError: 500,
};

const dataDir = process.env.VAULTLINE_DATA_DIR;
const port = Number(process.env.VAULTLINE_PORT);
const { log, state, bodiesDir } = recover(dataDir);

function commit(plan) {
  if (plan.error || plan.record === null) return plan;
  if (!log.append(plan.record)) return { error: 'InternalError' };
  S.apply(state, plan.record);
  return plan;
}

function sendJson(res, status, obj, headers = {}) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  res.writeHead(status, {
    ...headers, 'Content-Type': 'application/json', 'Content-Length': body.length,
  });
  res.end(body);
}

function sendError(res, code, headers) {
  sendJson(res, STATUS[code], { error: { code, message: code } }, headers);
}

function markerHeaders(v) {
  return {
    'X-Vaultline-Delete-Marker': 'true',
    'X-Vaultline-Version-Id': v.id,
    'X-Vaultline-Last-Modified': iso(v.lm),
  };
}

// Reads the whole request body. Resolves to a Buffer, or null if the client
// closed the connection before sending all of it.
async function readBody(req) {
  const chunks = [];
  try {
    for await (const c of req) chunks.push(c);
  } catch {
    return null;
  }
  return req.complete ? Buffer.concat(chunks) : null;
}

// Reads and discards the body, then responds, so a client that sends the
// whole declared body always receives the response.
async function discardThen(req, respond) {
  try {
    for await (const c of req) void c;
  } catch {
    return;
  }
  if (req.complete) respond();
}

function removeFile(file) {
  fs.unlink(file, () => {});
}

// --- buckets ---

function listBuckets(res) {
  const buckets = [...state.buckets.keys()]
    .sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))
    .map((name) => ({ name }));
  sendJson(res, 200, { buckets });
}

function createBucket(res, name) {
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  const p = commit(S.planCreateBucket(state, name));
  if (p.error) return sendError(res, p.error);
  sendJson(res, 201, { name });
}

function deleteBucket(res, name) {
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  const p = commit(S.planDeleteBucket(state, name));
  if (p.error) return sendError(res, p.error);
  sendJson(res, 200, { name });
}

function getVersioning(res, name) {
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  const bucket = state.buckets.get(name);
  if (!bucket) return sendError(res, 'NoSuchBucket');
  sendJson(res, 200, { status: bucket.status });
}

async function putVersioning(req, res, name) {
  const body = await readBody(req);
  if (body === null) return;
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  let doc;
  try {
    doc = JSON.parse(body.toString('utf8'));
  } catch {
    return sendError(res, 'InvalidArgument');
  }
  if (doc === null || typeof doc !== 'object' || Array.isArray(doc)) return sendError(res, 'InvalidArgument');
  if (doc.status !== 'Enabled' && doc.status !== 'Suspended') return sendError(res, 'InvalidArgument');
  const p = commit(S.planSetVersioning(state, name, doc.status));
  if (p.error) return sendError(res, p.error);
  sendJson(res, 200, { status: state.buckets.get(name).status });
}

// --- objects ---

async function putObject(req, res, name, params) {
  const key = decodeKey(params.get('key'));
  let err = null;
  if (!validBucketName(name)) err = 'InvalidBucketName';
  else if (key === null) err = 'InvalidKey';
  else if (Number(req.headers['content-length'] || 0) > MAX_BODY) err = 'EntityTooLarge';
  else if (!state.buckets.has(name)) err = 'NoSuchBucket';
  if (err) return discardThen(req, () => sendError(res, err));

  const body = crypto.randomUUID();
  const file = path.join(bodiesDir, body);
  const hash = crypto.createHash('md5');
  let size = 0;
  try {
    await pipeline(req, async function* (src) {
      for await (const c of src) {
        hash.update(c);
        size += c.length;
        yield c;
      }
    }, fs.createWriteStream(file, { flags: 'wx' }));
  } catch {
    removeFile(file);
    if (req.complete && !res.headersSent) sendError(res, 'InternalError');
    return;
  }
  if (!req.complete) return removeFile(file);

  const obj = {
    etag: '"' + hash.digest('hex') + '"', size, body,
    ctype: req.headers['content-type'] ?? 'application/octet-stream',
  };
  const p = commit(S.planPutObject(state, name, key, obj, Date.now()));
  if (p.error) {
    removeFile(file);
    return sendError(res, p.error);
  }
  const r = p.record;
  sendJson(res, 200, { key, versionId: r.id, etag: r.etag, size: r.size, lastModified: iso(r.lm) });
}

// Resolves the target of GET/HEAD: { error, headers } or { version }.
function resolveTarget(name, params) {
  const key = decodeKey(params.get('key'));
  if (!validBucketName(name)) return { error: 'InvalidBucketName' };
  if (key === null) return { error: 'InvalidKey' };
  const bucket = state.buckets.get(name);
  if (!bucket) return { error: 'NoSuchBucket' };
  const entry = bucket.keys.get(key);
  if (!params.has('versionId')) {
    const v = entry && entry.history[0];
    if (!v) return { error: 'NoSuchKey' };
    if (v.marker) return { error: 'NoSuchKey', headers: markerHeaders(v) };
    return { version: v };
  }
  const id = decodeUtf8(params.get('versionId'));
  const v = entry && id !== null && entry.history.find((x) => x.id === id);
  if (!v) return { error: 'NoSuchVersion' };
  if (v.marker) return { error: 'MethodNotAllowed', headers: markerHeaders(v) };
  return { version: v };
}

function getObject(req, res, name, params) {
  const t = resolveTarget(name, params);
  if (t.error) return sendError(res, t.error, t.headers);
  const v = t.version;
  res.writeHead(200, {
    ETag: v.etag,
    'Content-Length': v.size,
    'Content-Type': v.ctype,
    'X-Vaultline-Version-Id': v.id,
    'X-Vaultline-Last-Modified': iso(v.lm),
  });
  if (req.method === 'HEAD') return res.end();
  // Bodies are never unlinked while running, so the file is still there.
  pipeline(fs.createReadStream(path.join(bodiesDir, v.body)), res).catch(() => res.destroy());
}

function deleteObject(res, name, params) {
  const key = decodeKey(params.get('key'));
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  if (key === null) return sendError(res, 'InvalidKey');
  if (!params.has('versionId')) {
    const p = commit(S.planDeleteKey(state, name, key, Date.now()));
    if (p.error) return sendError(res, p.error);
    const marker = p.record ? p.record.marker : null;
    return sendJson(res, 200, { versionId: marker ?? 'null', deleteMarker: marker !== null });
  }
  const id = decodeUtf8(params.get('versionId'));
  if (!state.buckets.has(name)) return sendError(res, 'NoSuchBucket');
  if (id === null) return sendError(res, 'NoSuchVersion');
  const p = commit(S.planDeleteVersion(state, name, key, id));
  if (p.error) return sendError(res, p.error);
  sendJson(res, 200, { versionId: id, deleteMarker: p.version.marker });
}

// --- listings ---

function list(res, name, params, versions) {
  if (!validBucketName(name)) return sendError(res, 'InvalidBucketName');
  const opt = (n) => {
    if (!params.has(n)) return '';
    return decodeUtf8(params.get(n));
  };
  const prefix = opt('prefix');
  const delimiter = opt('delimiter');
  if (prefix === null || delimiter === null) return sendError(res, 'InvalidArgument');
  let maxKeys = 1000;
  if (params.has('maxKeys')) {
    const s = decodeUtf8(params.get('maxKeys'));
    if (s === null || !/^[1-9][0-9]{0,3}$/.test(s) || Number(s) > 1000) return sendError(res, 'InvalidArgument');
    maxKeys = Number(s);
  }
  let startAfter;
  if (!versions && params.has('startAfter')) {
    startAfter = decodeUtf8(params.get('startAfter'));
    if (startAfter === null) return sendError(res, 'InvalidArgument');
    if (params.has('token')) return sendError(res, 'InvalidArgument');
  }
  let place = null;
  if (params.has('token')) {
    const s = decodeUtf8(params.get('token'));
    place = s === null ? null : decodeToken(s, { e: versions ? 'v' : 'o', b: name, p: prefix, d: delimiter });
    if (place === null) return sendError(res, 'InvalidToken');
  }
  const bucket = state.buckets.get(name);
  if (!bucket) return sendError(res, 'NoSuchBucket');
  sendJson(res, 200, listPage(bucket, { versions, prefix, delimiter, maxKeys, startAfter, place }));
}

// --- routing ---

async function route(req, res) {
  const { segments, params } = parseTarget(req.url);
  const m = req.method;
  if (segments[0] === 'health' && segments.length === 1 && m === 'GET') {
    return sendJson(res, 200, { status: 'ok' });
  }
  if (segments[0] === 'buckets') {
    if (segments.length === 1 && m === 'GET') return listBuckets(res);
    const name = segments[1];
    if (segments.length === 2) {
      if (m === 'PUT') return createBucket(res, name);
      if (m === 'DELETE') return deleteBucket(res, name);
    } else if (segments.length === 3) {
      switch (segments[2]) {
        case 'versioning':
          if (m === 'GET') return getVersioning(res, name);
          if (m === 'PUT') return putVersioning(req, res, name);
          break;
        case 'object':
          if (m === 'PUT') return putObject(req, res, name, params);
          if (m === 'GET' || m === 'HEAD') return getObject(req, res, name, params);
          if (m === 'DELETE') return deleteObject(res, name, params);
          break;
        case 'objects':
          if (m === 'GET') return list(res, name, params, false);
          break;
        case 'versions':
          if (m === 'GET') return list(res, name, params, true);
          break;
        default:
      }
    }
  }
  sendError(res, 'InvalidArgument');
}

const server = http.createServer({ maxHeaderSize: 65536 }, (req, res) => {
  route(req, res).catch(() => {
    if (!res.headersSent) sendError(res, 'InternalError');
    else res.destroy();
  });
});
server.listen(port, '127.0.0.1');
