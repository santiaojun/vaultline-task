'use strict';
// Startup recovery. Every step is idempotent, so a kill at any point is
// followed by a rerun that reaches the same state.
const fs = require('node:fs');
const path = require('node:path');
const { Log } = require('./log');
const { createState, apply } = require('./state');

function recover(dataDir) {
  const bodiesDir = path.join(dataDir, 'bodies');
  fs.mkdirSync(bodiesDir, { recursive: true });

  // 1-2. Longest valid prefix of the log; any torn tail is truncated.
  const log = new Log(dataDir);
  const state = createState();
  for (const record of log.recover()) apply(state, record);

  // 3. Remove body files no surviving version references.
  const referenced = new Set();
  for (const bucket of state.buckets.values()) {
    for (const entry of bucket.order) {
      for (const v of entry.history) if (v.body) referenced.add(v.body);
    }
  }
  for (const name of fs.readdirSync(bodiesDir)) {
    if (referenced.has(name)) continue;
    try {
      fs.unlinkSync(path.join(bodiesDir, name));
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
  }
  return { log, state, bodiesDir };
}

module.exports = { recover };
