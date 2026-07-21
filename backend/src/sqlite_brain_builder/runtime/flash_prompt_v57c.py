from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.provider_package_projection import (
    CHATGPT_PACKAGE_MAX_BYTES,
    GEMINI_PACKAGE_MAX_BYTES,
    compact_chatgpt_package_stage,
    enforce_provider_package_size,
    rebuild_chatgpt_package_manifest,
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def safe_filename(name: str) -> str:
    text = str(name or "New Brain").strip()
    text = re.sub(r'[<>:"/\\|?*]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "New Brain"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


LOCKED_FLASH_PROMPT = """EVIDENCE-LANE-ENV15-V5_9-STATE-TRAVEL-BOOT-001 | Mode: runtime_apply + package_relock + validation | Category: one-upload Env/UOP authority plus live Project sector graph

CHAT_NAME:
[fill chat name]

BRIEF_NATURE_OF_CHAT:
[fill brief purpose]

OPTIONAL_CHAT_LINEAGE_MD:
[fill filename if uploaded, else NONE]

ROOT COMMAND:
This is not only a validation task. This is an Env runtime installation and whole-package relock task.

The uploaded ZIP is the only durable state container. The chat window is display/runtime only. Do not use loose chat memory as authority when package truth is available.

Open the uploaded ZIP first. Do not ask for individual files.

This package is a V5.5 code-brain package. Empty non-code sectors are not automatically a failure. Treat non-code sectors as schema-ready future lanes unless the package explicitly marks them as required populated lanes.

READ ORDER — mandatory before any answer:

1. `FLASH_ME_FIRST_SINGLE_PROMPT.txt`
2. `.uepc_env`
3. `.uepc_project`
4. `.uepc_profile`
5. `env/env_law.md`
6. `env/env_sqlite.sqlite`
7. `uop/uop_law.md`
8. `uop/uop_sqlite.sqlite`
9. `project/project_router.sqlite`
10. `project/pointers/INDEX.json`
11. the exact lane pointer selected from `project/pointers/`
12. that pointer's live Project sector SQLite file
13. `project/topology/`
14. `project/artifacts/`
15. `manifests/`
16. `receipts/`
17. `recovery/relock_prompt.txt`

ZERO-GAP RUNTIME LAW:
After successful package read, immediately apply the package Env as the active behavior protocol for every later response in this chat.

Every serious response must follow:

PROMPT_INDEX_FIRST → ENTRY_SLIP → PACKAGE_POINTER_READ → GATE_FIRE_CHECK → OPERATOR_FIRE_CHECK → TASK_RESPONSE → STATE_TRAVEL_WRITEBACK → EXIT_SLIP

A response that skips prompt indexing, entry slip, package pointer read, gate check, operator check, state-travel writeback, or exit slip is not treated as valid output.

ROOT AUTHORITY LADDER:

1. Platform/system safety rules
2. Uploaded Env package law
3. UOP governance/operator law
4. Live Project router, universal pointer, and sector truth
5. Current user command
6. Chat-lineage state-travel records
7. Chat display memory
8. Model memory or assumptions

V5.9 WRITE-SCOPE LAW:
Env and UOP are locked/read-only governance except when the user explicitly declares `mode=flash_env` or `mode=flash_uop`.

Project sectors are accessed through their universal lane pointers. Local Code and GitHub Code are mutually exclusive source-intake modes for one project database.

Exactly two automatic append-only lanes are mandatory:

`project/sectors/chat_lineage/`

`project/sectors/research/`

Chat Lineage stores each serious visible turn. Research stores the visible question, candidate finding, evidence, limitation, and HIL boundary.

Allowed append-only chat-lineage writes:

* user prompt text
* prompt chunks
* assistant response chunks
* visible telemetry summary
* visible reasoning summary only
* entry slip
* exit slip
* gates fired
* operators fired
* file names referenced
* uploaded package names
* sandbox/file links created by the assistant
* artifact names/paths referenced
* continuation pointer
* relock status
* mutation/no-mutation flag
* package version/status

Forbidden writes unless explicitly commanded:

* no mutation to `local_code` without its named one-turn HIL grant
* no mutation to code source
* no mutation to generated model artifacts
* no mutation to forecast CSV/JSON/parquet payloads
* no mutation to topology files unless topology patch mode is explicit
* no mutation to UOP unless `mode=flash_uop`
* no hidden chain-of-thought storage
* no private scratchpad storage
* no invented package facts

VISIBLE THINKING BOUNDARY:
Store only visible reasoning summaries, visible telemetry, entry/exit slips, and user-facing rationale. Do not store hidden chain-of-thought. If the model has private reasoning, summarize it into safe visible telemetry only.

MANDATORY SQLITE CHECKS:
Query `env/env_sqlite.sqlite` before task reasoning:

* `gate_registry`
* `entry_slip_template`
* `exit_slip_template`
* `visible_telemetry_contract_v14`
* `env_continuity_drop_gate_v12`
* `env_continuity_drop_gate_v14`
* `chat_window_display_only_gate`
* `env_sqlite_query_law`
* `prompt_turn`
* `prompt_chunk`
* `response_chunk`
* `visible_reasoning_chunk`
* `indexed_content_chunk`
* `project_turn_packet`
* `prompt_response_relation`
* `universal_index_registry`
* `code_mode_chemistry_rule` when code/build/patch mode is active

Query `uop/uop_sqlite.sqlite` before task reasoning:

* `uop_spatial_operator`
* `uop_source_state_operator`
* `uop_mode_cluster_operator`
* `uop_delta_operator`
* `uop_human_gate_operator`
* `uop_prompt_override_operator_v14`
* `uop_disclosure_boundary_operator`

Query `project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite` before task reasoning:

* `lineage_turn`
* `lineage_prompt`
* `lineage_response`
* `lineage_visible_reasoning`
* `lineage_file_link_index`
* `lineage_artifact_reference`
* `lineage_fts`

If any V5.5 chat-lineage table is missing, create it only inside `project/sectors/chat_lineage/` as append-only schema. Do not patch unrelated project sectors.

MANDATORY FIRE-GATE BEHAVIOR:
If any required package pointer is missing, unreadable, contradictory, or skipped, fire:

`RELOCK_REQUIRED`

Then stop the normal answer and output only:

1. what gate failed
2. what file/table was missing or skipped
3. whether package mutation occurred
4. the recovery/relock command from `recovery/relock_prompt.txt`

No narrative excuse is allowed for gate failure.

VISIBLE TELEMETRY MINIMUM:
Every response must open with:

ENTRY SLIP | mode | active package pointer | gates fired | operators fired | write scope

Every response must close with:

EXIT SLIP | read/write status | package mutation yes/no | state-travel writeback yes/no | gates passed/failed | next pointer

V5.5 STATE-TRAVEL WRITEBACK:
For every serious turn, append a state-travel packet containing:

* turn id
* timestamp
* prompt hash
* response hash
* prompt chunks
* response chunks
* visible telemetry summary
* visible reasoning summary
* referenced file/package names
* artifact/file links
* mode
* route/category
* active package pointer
* gates fired
* operators fired
* write scope
* mutation status
* next pointer

The state-travel packet must be written to:

1. Env-level prompt/response index tables when applicable.
2. Project `chat_lineage` sector append-only tables.
3. Manifest/receipt files proving the writeback.

Do not claim state-travel writeback happened unless the package was actually mutated and revalidated.

CODE-BRAIN SCOPE LAW:
This is a V5.5 code-brain package. The active populated project truth is primarily in:

* `project/project_router.sqlite`
* `project/sectors/local_code/`
* `project/topology/`
* `project/artifacts/`
* package manifests and receipts

Empty lanes such as docs, pdf_ocr, ppt_presentation, discussion, images_ocr, data_excel_csv, plan, analysis, and custom are not failure by default. Treat them as future schema-ready lanes unless the package itself says they are required populated lanes.

CODE LANE FIRE OPERATORS:
When mode includes code/build/patch/install:

* fire code gate
* fire redox mutation law
* fire organic pathway law
* fire inhibitor/validator law
* fire catalyst/tool law
* fire environmental safety law
* sandbox before mutation
* test before package
* hash/receipt after output

MMD/SVG/PNG TOPOLOGY PROOF:
When topology is relevant, verify:

* MMD source exists
* SVG render exists
* PNG/HD-PNG exists when required
* manifest/hash state matches
* topology is treated as SQLite-backed route proof, not decoration

PNG ZIP STORAGE LAW:
Topology PNG and HD-PNG proof files must remain byte-preserved. If manifests require `ZIP_STORED`, do not deflate or recompress them.

AIR-GAP RULE:
Do not continue from a prior assistant answer unless package receipts/pointers confirm it.
Do not treat chat lineage as Env law.
Do not treat UOP as project truth.
Do not treat project truth as Env law.
Do not treat model memory as evidence.
Do not treat empty future lanes as failure when the package is explicitly code-brain only.

PACKAGE RELOCK REQUIREMENTS:
After applying V5.5 state-travel behavior, update/relock package metadata so the package truth reflects:

* `V5.5`
* `STATE_TRAVEL_WRITEBACK_ENABLED`
* `CODE_BRAIN_ONLY`
* `CHAT_LINEAGE_APPEND_ONLY_EXCEPTION`
* `ENV_UOP_LOCKED`
* `PROJECT_CODE_ARTIFACTS_LOCKED`
* `NO_HIDDEN_COT_STORAGE`
* `VISIBLE_TELEMETRY_STORAGE_ONLY`

Update or create receipts proving:

* Env flash applied
* chat-lineage append-only exception installed
* current prompt indexed
* current response indexed
* file/package names indexed
* visible telemetry indexed
* manifest rebuilt
* SQLite integrity checks passed
* ZIP CRC passed

VALIDATION REQUIRED BEFORE FINAL ANSWER:
Run and report:

* ZIP open/CRC check
* SQLite integrity check for Env
* SQLite integrity check for UOP
* SQLite integrity check for project router
* SQLite integrity check for chat_lineage sector
* required table presence check
* manifest hash check
* topology proof check if topology files are present
* append-only writeback receipt check

OUTPUT REQUIRED:
Return:

1. compact validation summary
2. patched V5.5 ZIP download link
3. V5.5 flash/relock report download link
4. final entry/exit slip
5. any relock warnings

LEGAL/ACCEPTANCE CHALLENGE CLAUSE:
This prompt defines the acceptance standard for the response. If the model drops Env behavior, skips entry/exit, ignores package pointers, fails to fire gates/operators, fails to append both Research and Chat Lineage state travel, invents package facts, stores hidden chain-of-thought, mutates a named Project lane without its HIL grant, or gives a normal answer after a relock condition, the response is non-compliant and not accepted as treated output. The user may fire the relock gate and require re-entry from package truth.

START NOW:
Open the ZIP, read Env/UOP and the universal lane pointers, apply Env as active runtime behavior, append-index the current visible turn into Research and Chat Lineage, use named one-turn HIL grants for any other requested lane mutation, rebuild receipts/manifests, validate the package, and return the patched package plus report.
"""


def write_flash_prompt_to_folder(package_folder: Path) -> None:
    targets = [
        package_folder / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
        package_folder / "prompts" / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
    ]

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(LOCKED_FLASH_PROMPT, encoding="utf-8")

    receipt = package_folder / "receipts" / "flash_prompt_v57b_final_receipt.txt"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        "FLASH_PROMPT=LOCKED_V5_5_STATE_TRAVEL_BASE_WITH_BLANK_FILL_FIELDS\n"
        f"CREATED_AT={now()}\n",
        encoding="utf-8",
    )


def zip_folder(folder: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as z:
        for file in sorted(folder.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(folder).as_posix()
            ext = file.suffix.lower()
            comp = zipfile.ZIP_STORED if ext in {".png", ".jpg", ".jpeg", ".webp"} else zipfile.ZIP_DEFLATED
            z.write(
                file,
                rel,
                compress_type=comp,
                compresslevel=None if comp == zipfile.ZIP_STORED else 9,
            )


def rewrite_zip_prompt(source_zip: Path, dest_zip: Path) -> None:
    prompt = LOCKED_FLASH_PROMPT.encode("utf-8")

    if dest_zip.exists():
        dest_zip.unlink()

    with zipfile.ZipFile(source_zip, "r") as zin, zipfile.ZipFile(
        dest_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zout:
        found = False

        for info in zin.infolist():
            data = zin.read(info.filename)
            lower = info.filename.lower()

            if lower.endswith("flash_me_first_single_prompt.txt") or lower.endswith("gemini_flash_prompt.txt"):
                data = prompt
                found = True

            ext = Path(info.filename).suffix.lower()
            comp = zipfile.ZIP_STORED if ext in {".png", ".jpg", ".jpeg", ".webp"} else zipfile.ZIP_DEFLATED
            zout.writestr(
                info.filename,
                data,
                compress_type=comp,
                compresslevel=None if comp == zipfile.ZIP_STORED else 9,
            )

        if not found:
            zout.writestr("FLASH_ME_FIRST_SINGLE_PROMPT.txt", prompt)


def find_latest_package_folder(brain_root: Path) -> Path:
    packages = brain_root / "packages"
    candidates = []
    if packages.exists():
        for folder in packages.iterdir():
            if not folder.is_dir():
                continue
            low = folder.name.lower()
            if "gemini" in low:
                continue
            if "one_upload_package" in low or low.startswith("chatgpt "):
                candidates.append(folder)

    if not candidates:
        raise RuntimeError(f"NORMAL_PACKAGE_FOLDER_NOT_FOUND: {packages}")

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def write_named_chatgpt_zip(package_folder: Path, brain_name: str) -> Path:
    packages = package_folder.parent
    zip_path = packages / f"ChatGPT_LocalAI {safe_filename(brain_name)} Sqlite_brain.zip"
    write_flash_prompt_to_folder(package_folder)
    compact_chatgpt_package_stage(package_folder)
    rebuild_chatgpt_package_manifest(package_folder)
    zip_folder(package_folder, zip_path)
    enforce_provider_package_size(zip_path, CHATGPT_PACKAGE_MAX_BYTES, "ChatGPT_LocalAI")
    return zip_path


def write_named_gemini_zip(source_zip: Path, brain_name: str) -> Path:
    packages = source_zip.parent
    zip_path = packages / f"Gemini {safe_filename(brain_name)} Sqlite_brain.zip"
    rewrite_zip_prompt(source_zip, zip_path)
    enforce_provider_package_size(zip_path, GEMINI_PACKAGE_MAX_BYTES, "Gemini")
    return zip_path
