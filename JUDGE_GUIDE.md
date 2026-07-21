# Evidence Lane judge guide

This is the bounded evaluation path for the private OpenAI Build Week source repository.

## 1. Obtain the application

Download the Windows artifacts from:

https://drive.google.com/drive/folders/1fRb_FE5EBzGGqZYwdRWtqH6Y2TAAJLfG?usp=sharing

Published SHA-256 values:

| Artifact | SHA-256 |
|---|---|
| Installer | `A7741AC3425B6F7A679C64147F015C96592D96565FBAAC5297FB245D5EC95C8E` |
| Standalone candidate | `4A2E8F3F6F608BA7652E1B9774C6D7088B1CC541661C5A759AC8A2F27D4E585C` |

Verify in PowerShell:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\downloaded-file.exe'
```

The installer is unsigned. An unknown-publisher warning is expected; stop if the hash does not match.

## 2. Install or launch

Installer path:

1. Run the hash-matched installer.
2. Use the standard current-user Next/Install/Finish flow.
3. Launch Evidence Lane from the installed shortcut.

Standalone path:

1. Run the hash-matched standalone executable.
2. No development server or external Python console should be required for normal launch.

## 3. Bounded package demonstration

Use only a public-safe local project folder.

1. Create or select a project brain.
2. Open **Source Intake**.
3. Select **Local Code**.
4. Choose **Open Folder** and select the public-safe project.
5. Confirm the listed sources are within that project.
6. Run **Build Command**.
7. Open **Brain Output**, then the package output.
8. Inspect the current pointer, file/source manifest, hashes, and creation receipt.

The expected evidence is a bounded packet, not a claim that the model automatically owns or remembers the project.

## 4. Same-question comparison

Ask this in one new chat without a package and a second new chat with the validated packet:

> What is the current verified project state, which source files support it, and what decision is still open? Use only supplied project evidence; if none is supplied, say so.

A pass requires the packet-backed response to cite the supplied pointer and source records. A fluent guess is not a pass.

## 5. Human gate

Evidence Lane separates review from promotion. The backend can record `APPROVE`, `REJECT`, or `SUPERSEDE`; promotion is a separate governed action requiring a verified approval receipt. Do not treat a generated candidate or UI animation as accepted state.

## 6. 3D Telemetry boundary

The video may show the 3D Telemetry surface as a visual preview. Full lifecycle, interaction, and performance claims are outside this repository's current proof boundary.

## 7. Known defect to observe

The bundled public-model Env package is mixed/outdated relative to the intended final Env contract. Do not use that package to infer that the Env architecture is final. This limitation is disclosed in the repository and should be treated as a candidate defect, not hidden as a pass.

## 8. Build Week feedback session

Primary selected session:

`019f6e61-234c-7d30-84d8-aa2b497c3896`

Codex and GPT-5.6 Sol at Ultra reasoning were used in that task for package, handoff, embedded-backend, desktop, and approval-receipt implementation/debugging. Selection was based on demonstrable overlap, not token volume.
