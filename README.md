# Vaultline task

A greenfield task: the candidate builds Vaultline, a versioned object storage
service in Node.js (versions, delete markers, prefix/delimiter listing with
pagination, crash recovery), starting from a scaffold that holds only
`config.toml`.

```text
task.toml               metadata, limits, verifier settings
instruction.md          requirements given to the candidate
app/                    candidate scaffold (config.toml only)
environment/Dockerfile  Node.js 20.20.2 + python3
reference-solution/     complete implementation, expected to score 100
verifier/
  tests/                scored black-box checks (run_tests.py)
  scoring.yml           check IDs and points
  mutants/              one-defect patches and their verification records
proof-of-work/          model runs and their verification records
```

Only `instruction.md` and `app/` are candidate-visible.
