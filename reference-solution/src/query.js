'use strict';
// Request target parsing and validation (§3.1, §3.2, §4). Query values are
// kept raw and decoded only when an endpoint uses them.

const utf8Decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

function hexVal(c) {
  if (c >= 0x30 && c <= 0x39) return c - 0x30;
  if (c >= 0x41 && c <= 0x46) return c - 0x37;
  if (c >= 0x61 && c <= 0x66) return c - 0x57;
  return -1;
}

// One left-to-right pass: `+` -> space, `%XX` -> byte, anything else as is.
function decodeBytes(raw) {
  const src = Buffer.from(raw, 'latin1');
  const out = Buffer.allocUnsafe(src.length);
  let n = 0;
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (c === 0x2b) {
      out[n++] = 0x20;
    } else if (c === 0x25 && i + 2 < src.length &&hexVal(src[i + 1]) >= 0 && hexVal(src[i + 2]) >= 0) {
      out[n++] = hexVal(src[i + 1]) * 16 + hexVal(src[i + 2]);
      i += 2;
    } else {
      out[n++] = c;
    }
  }
  return out.subarray(0, n);
}

// Decoded value as a string, or null if it is not valid UTF-8.
function decodeUtf8(raw) {
  try {
    return utf8Decoder.decode(decodeBytes(raw));
  } catch {
    return null;
  }
}

function parseTarget(url) {
  const q = url.indexOf('?');
  const pathname = q < 0 ? url : url.slice(0, q);
  const params = new Map();
  if (q >= 0) {
    for (const part of url.slice(q + 1).split('&')) {
      if (part === '') continue;
      const eq = part.indexOf('=');
      const name = decodeUtf8(eq < 0 ? part : part.slice(0, eq));
      if (name === null || params.has(name)) continue;
      params.set(name, eq < 0 ? '' : part.slice(eq + 1));
    }
  }
  return { segments: pathname.split('/').slice(1), params };
}

function validBucketName(name) {
  return /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(name);
}

// Returns the key string, or null if `raw` is not a valid key.
function decodeKey(raw) {
  if (raw === undefined) return null;
  const key = decodeUtf8(raw);
  if (key === null) return null;
  const len = Buffer.byteLength(key, 'utf8');
  if (len < 1 || len > 1024) return null;
  if (/[\u0000-\u001f\u007f]/.test(key)) return null;
  return key;
}

module.exports = { parseTarget, decodeUtf8, validBucketName, decodeKey };
