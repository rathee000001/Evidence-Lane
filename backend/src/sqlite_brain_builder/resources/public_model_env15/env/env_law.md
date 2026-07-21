# UEPC Env15 Public Environment Law

Env15 is a standalone package-first governance environment.

- Env: public-readable, normal-turn read-only, `flash_env` mutation only.
- UOP: public governance-readable, normal-turn read-only, `flash_uop` mutation only.
- Project base/router and all 14 sector databases: public-readable.
- Chat Lineage: the only automatic append-only write lane.
- Other project sectors: explicit named one-turn mutation, receipt, immutable snapshot and immediate relock.
- State travel: PREPARE → RESPONSE → COMMIT with exact visible prompt/output, links, receipts, hashes and canonical lineage head.
- Code intelligence: current Code Snapshot plus reverse Git Lineage with exact changed files, patch hunks and impact mapping.
- Version control: immutable hashed snapshots; rollback verifies hash and restores to a new versioned folder without overwriting current/history.
- MMD: user-prompt start, complete end-to-end flow, accepted wide fixed layout, no global reflow, both SVG and PNG required.
