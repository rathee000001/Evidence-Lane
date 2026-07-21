# Evidence OS clean accepted frontend and theme contract

This tree is the clean native-frontend source for the accepted T023 cockpit. It
contains one UI composition only: `src/App.tsx` renders the exact accepted
`EvidenceOSCockpitSimulation` from `theme-contract/`.

`theme-contract/` is a byte-for-byte source copy of the accepted UI at
the accepted UI contract source (generated/runtime directories are
excluded). Runtime commands are native-only: the Tauri window installs
`src/nativeAcceptedUiTransport.ts`, which sends the action registry commands
through the persistent `worker_command` backend. There is no HTTP command
bridge, browser simulator fallback, or second desktop-shell UI implementation
that can drift from the installed application.

The native shell source is under `src-tauri/`. The clean backend is deliberately
provided as the separate sibling `evidence-os-clean-backend` tree.

The Public V1 native standalone/update-installer HIL source has been verified
with:

```powershell
npm ci
npm exec tsc -- --noEmit
npm run build
cargo check --features embedded-worker
```

`CLEAN_SOURCE_MANIFEST.sha256` binds every payload file in deterministic path
order, and `CLEAN_SOURCE_VERIFICATION.json` records the exact proof state.
