from __future__ import annotations
from pathlib import Path


def generate_flash_prompt(package_root: str | Path, brain_name: str, brief: str, current_task: str) -> Path:
    root = Path(package_root)
    prompt = root / 'FLASH_ME_FIRST_SINGLE_PROMPT.txt'
    text = (
        "UEPC-SQLITE-BRAIN-PACKAGE-FLASH-001 | Mode: flash_package + validation | Category: one-upload project brain package\n\n"
        f"BRAIN_NAME:\n[{brain_name}]\n\n"
        f"BRIEF:\n[{brief}]\n\n"
        f"CURRENT_TASK_AFTER_FLASH:\n[{current_task}]\n\n"
        "I uploaded one SQLite Brain Builder V3 project brain package ZIP. Do not ask for individual files. The ZIP is the package container.\n\n"
        "First inspect and read:\n.uepc_env\n.uepc_uop\n.uepc_project\nenv/env_law.md\nuop/uop_law.md\nproject/project_router.sqlite\nproject/project_pointer.json\nmanifests/PACKAGE_CONTENTS.json\nreceipts/last_exit_slip.txt\n\n"
        "Treat the chat window as display only. Durable state is inside the package. Env and UOP are read-only law. Project sectors are mutable only by explicit user command and only if a new package is actually produced. Do not use ambient memory. Do not store hidden chain-of-thought. Read chat lineage/current-state pointer before answering. Run compact entry slip and continue from the current task.\n"
    )
    prompt.write_text(text, encoding='utf-8')
    (root / 'prompts').mkdir(exist_ok=True)
    (root / 'prompts' / 'FLASH_ME_FIRST_SINGLE_PROMPT.txt').write_text(text, encoding='utf-8')
    return prompt
