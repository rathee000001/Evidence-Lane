from __future__ import annotations
from pathlib import Path
import zipfile, shutil
from sqlite_brain_builder.core import now, file_sha256, write_json
from sqlite_brain_builder.packaging.flash_prompt_generator import generate_flash_prompt


def compile_one_upload_package(brain_root: str | Path, router_db: str | Path, package_zip: str | Path, brain_name: str, brief: str = 'Project brain', current_task: str = 'Continue from package pointer') -> dict:
    brain_root = Path(brain_root)
    router_db = Path(router_db)
    package_zip = Path(package_zip)
    root = brain_root / 'PROJECT_BRAIN_PACKAGE'
    if root.exists():
        shutil.rmtree(root)
    for d in ['env','uop','project/sectors','project/topology','manifests','receipts','prompts','recovery']:
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / '.uepc_env').write_text('ENV_LOCKED=true\nCHAT_WINDOW=DISPLAY_ONLY\nPROJECT_MUTATION_BY_COMMAND_ONLY=true\n', encoding='utf-8')
    (root / '.uepc_uop').write_text('UOP_LOCKED=true\nCAN_OVERRIDE_ENV=false\nCAN_OVERRIDE_PROJECT=false\n', encoding='utf-8')
    (root / '.uepc_project').write_text('PROJECT_SECTORS_MUTABLE_BY_EXPLICIT_COMMAND=true\n', encoding='utf-8')
    (root / 'env' / 'env_law.md').write_text('# Env Law\nRead-only package constitution. Chat is display only.\n', encoding='utf-8')
    (root / 'uop' / 'uop_law.md').write_text('# UOP Law\nRead-only user/profile governance.\n', encoding='utf-8')
    (root / 'project' / 'project_law.md').write_text('# Project Law\nProject sectors are mutable only by explicit command.\n', encoding='utf-8')
    shutil.copy2(router_db, root / 'project' / 'project_router.sqlite')
    sectors_src = brain_root / 'sectors'
    if sectors_src.exists():
        for p in sectors_src.rglob('*'):
            if p.is_file():
                dest = root / 'project' / 'sectors' / p.relative_to(sectors_src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
    renders_src = brain_root / 'renders'
    if renders_src.exists():
        for p in renders_src.rglob('*'):
            if p.is_file():
                dest = root / 'project' / 'topology' / p.relative_to(renders_src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
    (root / 'README_NEXT_PROMPT.txt').write_text('Upload this package and run FLASH_ME_FIRST_SINGLE_PROMPT.txt.\n', encoding='utf-8')
    generate_flash_prompt(root, brain_name, brief, current_task)
    contents = []
    for p in sorted(root.rglob('*')):
        if p.is_file():
            contents.append({'path': str(p.relative_to(root)), 'size': p.stat().st_size, 'sha256': file_sha256(p)})
    write_json(root / 'manifests' / 'PACKAGE_CONTENTS.json', contents)
    (root / 'manifests' / 'INTERNAL_HASH_MANIFEST.txt').write_text('\n'.join(f"{x['sha256']}  {x['size']}  {x['path']}" for x in contents), encoding='utf-8')
    receipt = f"EXIT SLIP: PACKAGE_BUILT | brain={brain_name} | files={len(contents)} | time={now()}\n"
    (root / 'receipts' / 'last_exit_slip.txt').write_text(receipt, encoding='utf-8')
    (root / 'receipts' / 'build_receipt.md').write_text('# Build Receipt\n\n' + receipt, encoding='utf-8')
    package_zip.parent.mkdir(parents=True, exist_ok=True)
    if package_zip.exists():
        package_zip.unlink()
    with zipfile.ZipFile(package_zip, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(root))
    with zipfile.ZipFile(package_zip) as z:
        bad = z.testzip()
    return {'package_zip': str(package_zip), 'sha256': file_sha256(package_zip), 'testzip': bad, 'file_count': len(contents)}
