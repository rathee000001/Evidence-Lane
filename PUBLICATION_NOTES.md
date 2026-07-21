# Publication notes

This private repository was prepared from the clean full-app source authority without modifying that authority.

## Excluded generated material

- `node_modules/`
- frontend `dist/`
- Rust/Tauri `target/`
- `build-output/`
- Python caches and bytecode
- compiled executables and installer outputs
- machine-local verification receipts and stale source-manifest seals

All excluded material is reproducible or machine-specific and is not source.

## Privacy-only sanitation

- Replaced private absolute workstation paths in tests and evidence tools with explicit command-line arguments or environment variables.
- Replaced a personal default UI profile label with `Evidence Lane Operator`.
- Replaced personal test-account names and mock secret strings with clearly test-only values.
- Preserved the full application source, runtime templates, SQLite resources, Env archives, build scripts, lock files, and test logic.

Host-bound optional audit inputs use these variables:

| Variable | Purpose |
|---|---|
| `EVIDENCE_LANE_ENV15_SOURCE_ARCHIVE` | Original Env15 archive parity audit |
| `EVIDENCE_LANE_ENDPOINT_SCHEMA` | External endpoint-contract schema audit |
| `EVIDENCE_LANE_INTEGRITY_EVIDENCE_MANIFEST` | External selected-brain evidence-manifest audit |
| `EVIDENCE_LANE_LEGACY_WORKSPACE` | Archived legacy frontend source audit |
| `EVIDENCE_LANE_UIUX_LAB` | Accepted UI lab source audit |

If these authorities are not supplied, only the corresponding host-bound audit is skipped; the self-contained application tests remain runnable.
