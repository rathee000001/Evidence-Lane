# User Manual

## First run
Choose a workspace directory. Create a local account. The app scans dependencies and writes only local workspace data.

## Brain sidebar
Select a brain from the left sidebar to restore its last state. Use **Open Brain Output Folder** to open the active brain folder.

## Category-first intake
Use Add Intake. Code lane accepts GitHub or local code folder only. Semantic lanes require schema contracts.

## No AI during build
The builder extracts, hashes, OCRs, chunks, indexes, generates MMDs, and receipts. AI interpretation happens later outside the build.

## Controlled quarantine
If integrity/HRI gates fire, the app enters read-only quarantine, preserves user data, writes local receipt, and blocks new builds/exports.
