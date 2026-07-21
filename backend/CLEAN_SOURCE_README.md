# Evidence OS clean tested backend source

This tree is the complete Python backend source used by the accepted T023 UI and
native transport. It includes the current runtime, SQL schemas, ENV15 resources,
packaging logic, project-local Delta 1..N ledger, native picker routes, connector
registry, boot/version lifecycle, and the curated self-contained backend tests.

Run the independent backend proof from this directory:

```powershell
python -m pytest -q
```

Runtime user data, credentials, environment files, build outputs, caches, and
compiled executables are excluded. `CLEAN_SOURCE_MANIFEST.sha256` binds every
payload file in deterministic path order; `CLEAN_SOURCE_VERIFICATION.json`
records the exact proof state.
