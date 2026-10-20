# Vaultline reference solution

A versioned object storage service that implements `../instruction.md`.
Node.js v20.20.2, built-in modules only, no setup step.

## Run

From this directory, with `VAULTLINE_PORT` and `VAULTLINE_DATA_DIR` set:

```sh
npm start
```

`config.toml` holds the same setup and run commands as the candidate
scaffold.

## Layout

- `src/server.js`: HTTP routing; each write is planned, appended to the
  log and applied in one synchronous step.
- `src/log.js`: append-only record log (length + CRC32 framing).
- `src/state.js`: in-memory state; the pure apply function and write plans.
- `src/recovery.js`: startup recovery (valid log prefix, torn-tail
  truncation, unreferenced body cleanup).
- `src/listing.js`: object and version listings, stateless tokens.
- `src/query.js`: query decoding, bucket name and key validation.

The verifier's behavior checks are in `../verifier/tests/`.
