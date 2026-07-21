from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise SystemExit(f"embedded worker is missing: {executable}")
    workspace = Path(tempfile.mkdtemp(prefix="evidence-os-embedded-worker-"))
    requests = [
        {"id": "packaged-ping", "command": "system.ping", "payload": {}},
        {
            "id": "packaged-workspace",
            "command": "workspace.init",
            "payload": {"workspace_dir": str(workspace)},
        },
    ]
    try:
        run = subprocess.run(
            [str(executable)],
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        messages = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        responses = {
            message.get("id"): message
            for message in messages
            if message.get("type") == "response"
        }
        ping = responses["packaged-ping"]
        assert ping["ok"] is True, ping
        assert ping["result"]["contract"] == "T023_FULL_APP_BACKEND_V2", ping
        assert ping["result"]["workspace_required"] is False, ping
        assert ping["result"]["frozen"] is True, ping
        assert ping["result"]["packaging_mode"] == "PYINSTALLER_EMBEDDED_WORKER", ping
        initialized = responses["packaged-workspace"]
        assert initialized["ok"] is True, initialized
        assert Path(initialized["result"]["workspace_db"]).is_file(), initialized
        print(
            json.dumps(
                {
                    "schema": "T023_EMBEDDED_WORKER_VALIDATION_V1",
                    "status": "PASS",
                    "worker_exe": str(executable),
                    "backend_contract": ping["result"]["contract"],
                    "frozen": ping["result"]["frozen"],
                    "workspace_init": "PASS",
                    "return_code": run.returncode,
                },
                indent=2,
            )
        )
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
