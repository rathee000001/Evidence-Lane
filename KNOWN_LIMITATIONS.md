# Known limitations

These boundaries are part of the candidate, not deferred fine print.

1. **Mixed/outdated Env package.** The bundled public-model Env resource is not the final current Env definition and contains mixed historical/current material. Its structure is retained because the application depends on the packaged resource, but it must not be described as the final Env contract.
2. **Git history and Svelte semantic coverage are bounded.** A GitHub-origin project loaded without the governed Git lane and authorized `.git` history does not reconstruct commit-level lineage or commit Deltas. In the observed Open WebUI packet, 584 `.svelte` records retained paths, sizes, and hashes but were classified as `UNSUPPORTED_HASH_ONLY`; full Svelte semantic content was not proven. Docker packaging is a separate boundary.
3. **Unsigned Windows artifacts.** The installer and standalone executable are not code-signed. Verify SHA-256 before launch.
4. **No universal-provider claim.** The code has provider-readable handoff paths, but this candidate does not prove identical behavior across every provider or hosted model.
5. **No controlled performance claim.** No token-savings, speedup, defect-reduction, or zero-rescan metric is claimed here.
6. **3D Telemetry is a preview.** It can be shown as product direction; the complete Full View, scene-travel, interaction, and cross-machine lifecycle are outside the narrow submission proof.
7. **Human promotion remains gated.** The backend implements explicit decisions and approval receipts, but the desktop's complete end-to-end promotion path is not represented as universally proven.
8. **Judge-machine variance remains possible.** Build and isolated-runtime evidence exists, and the installer has been installed by the project owner. A separate clean-machine matrix is not claimed.
9. **Some forensic tests are host-bound.** Tests that require archived external authority files skip unless their documented environment variables are supplied. They are retained as audit code rather than silently rewritten into self-contained proof.

Evidence that would change these boundaries: a corrected and resealed Env package; governed Git-lane intake with authorized history; full Svelte semantic parsing; signed artifacts; controlled provider and performance matrices; reproducible clean-machine receipts; and a fresh end-to-end HIL/3D lifecycle recording.
