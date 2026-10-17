'use strict';
// Append-only record log. Frame: [u32 LE length][u32 LE CRC32][UTF-8 JSON payload].
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const HEADER = 8;

function frame(record) {
  const payload = Buffer.from(JSON.stringify(record), 'utf8');
  const buf = Buffer.allocUnsafe(HEADER + payload.length);
  buf.writeUInt32LE(payload.length, 0);
  buf.writeUInt32LE(zlib.crc32(payload), 4);
  payload.copy(buf, HEADER);
  return buf;
}

// Returns the records of the longest valid prefix and its byte length.
function parse(buf) {
  const records = [];
  let off = 0;
  while (off + HEADER <= buf.length) {
    const len = buf.readUInt32LE(off);
    const crc = buf.readUInt32LE(off + 4);
    if (off + HEADER + len > buf.length) break;
    const payload = buf.subarray(off + HEADER, off + HEADER + len);
    if (zlib.crc32(payload) !== crc) break;
    let record;
    try {
      record = JSON.parse(payload.toString('utf8'));
    } catch {
      break;
    }
    records.push(record);
    off += HEADER + len;
  }
  return { records, validLength: off };
}

class Log {
  constructor(dataDir) {
    this.file = path.join(dataDir, 'log');
    if (!fs.existsSync(this.file)) fs.writeFileSync(this.file, '');
    this.fd = fs.openSync(this.file, 'r+');
    this.end = 0;
  }

  // Reads the valid prefix and truncates any torn tail. Idempotent.
  recover() {
    const buf = fs.readFileSync(this.file);
    const { records, validLength } = parse(buf);
    if (validLength < buf.length) fs.ftruncateSync(this.fd, validLength);
    this.end = validLength;
    return records;
  }

  // Appends one record. Returns true once every byte has reached the OS.
  // On failure the log is truncated back to its previous end and false is
  // returned; if truncation fails too, the process exits immediately.
  append(record) {
    const buf = frame(record);
    const start = this.end;
    try {
      let done = 0;
      while (done < buf.length) {
        const n = fs.writeSync(this.fd, buf, done, buf.length - done, start + done);
        if (n <= 0) throw new Error('short write');
        done += n;
      }
    } catch {
      try {
        fs.ftruncateSync(this.fd, start);
      } catch {
        process.exit(70);
      }
      return false;
    }
    this.end = start + buf.length;
    return true;
  }
}

module.exports = { Log, frame, parse };
