# Production backup and restore evidence

Date: 2026-09-01

Status: **verified on reviewed branch; merge required to activate the daily schedule**

## Scope

- Production data: `/root/photogen-data`
- Production outputs: `/root/photogen-outputs`
- Snapshot method: regular-file copy plus SQLite online backup API
- Integrity: manifest file set, byte size, SHA-256, and SQLite
  `PRAGMA integrity_check`
- Encryption: `age` to the reviewed Ashbi recovery SSH recipient before upload
- Off-host store: private GitHub Actions artifact with 14-day retention
- Local retention: seven days

The environment file and provider/deployment secrets are deliberately excluded.
They remain in the deployment secret inventory and require an independent
recovery procedure.

## Evidence

- Branch SHA: `99d3b410afa4cee3bf6a1944f501b022b85c642b`
- Workflow run:
  <https://github.com/camster91/creative-studio/actions/runs/33462661827>
- Encrypted artifact: `photogen-backup-33462661827-1`, artifact ID `9783718115`
- Artifact expiry: 2026-09-15T02:28:44Z
- Snapshot time: 2026-09-01T02:28:43.664831Z
- Files verified: 19
- Bytes verified: 188,252
- SQLite databases verified: 4
- Recorded isolated restore duration: under one second at current data size
- Transient remote tool and unencrypted snapshot directories: removed
- Local archive permissions: mode `0600`

The drill copied live WAL-mode databases, normalized the isolated copies to
rollback-journal mode, decrypted the encrypted archive on a fresh hosted
runner, extracted it into an isolated directory, matched every manifest entry,
recomputed hashes and sizes, and reran SQLite integrity checks.

## Recovery-key control

Recipient fingerprint:
`SHA256:+pJjM5Sq5bK9Xe2yvhwLc/BIWZuld5RNYEl5n4YqN8A`.

The current recovery identity is also authorized for deployment. This is a
documented residual concentration risk, not the desired long-term state. Add a
separate offline recovery recipient, prove a decrypt with it, and retain the
current identity until older artifacts expire before removing it.

## Decision

The backup/restore implementation is verified. The daily schedule is not yet
authoritative because the branch has not been merged to `main`; merging it is a
production-governed change because every `main` merge also invokes the staged
application deployment workflow.
