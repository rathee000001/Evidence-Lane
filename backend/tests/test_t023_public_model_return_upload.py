from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker


def _return_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"contract":"T023_PUBLIC_MODEL_RETURN_V1"}')
    return buffer.getvalue()


def test_public_model_return_upload_is_hash_named_bounded_and_idempotent(tmp_path: Path) -> None:
    raw = _return_zip()
    digest = hashlib.sha256(raw).hexdigest()
    payload = {
        "package_name": "chatgpt-return.zip",
        "package_base64": base64.b64encode(raw).decode("ascii"),
        "provider": "ChatGPT",
    }

    first = ipc_worker._stage_public_model_return_upload(tmp_path, "Research Brain", payload)
    second = ipc_worker._stage_public_model_return_upload(tmp_path, "Research Brain", payload)

    assert first == second
    assert first == tmp_path / ".evidenceos_delta_intake" / "research_brain" / f"chatgpt_{digest}.zip"
    assert first.read_bytes() == raw


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"package_name": "return.zip", "provider": "ChatGPT"}, "RETURN_PACKAGE_UPLOAD_BASE64_REQUIRED"),
        (
            {"package_name": "../return.zip", "package_base64": "UEs=", "provider": "ChatGPT"},
            "RETURN_PACKAGE_UPLOAD_NAME_INVALID",
        ),
        (
            {"package_name": "return.zip", "package_base64": "not base64!", "provider": "ChatGPT"},
            "RETURN_PACKAGE_UPLOAD_BASE64_INVALID",
        ),
        (
            {
                "package_name": "return.zip",
                "package_base64": base64.b64encode(b"not-a-zip").decode("ascii"),
                "provider": "ChatGPT",
            },
            "RETURN_PACKAGE_UPLOAD_ZIP_SIGNATURE_INVALID",
        ),
    ],
)
def test_public_model_return_upload_fails_closed(payload: dict[str, str], error: str, tmp_path: Path) -> None:
    with pytest.raises(ipc_worker.WorkerError, match=error):
        ipc_worker._stage_public_model_return_upload(tmp_path, "Research Brain", payload)


def test_public_model_return_upload_enforces_size_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ipc_worker, "_MAX_PUBLIC_MODEL_RETURN_UPLOAD_BYTES", 3)
    with pytest.raises(ipc_worker.WorkerError, match="RETURN_PACKAGE_UPLOAD_SIZE_INVALID"):
        ipc_worker._stage_public_model_return_upload(
            tmp_path,
            "Research Brain",
            {
                "package_name": "return.zip",
                "package_base64": base64.b64encode(b"PK12").decode("ascii"),
                "provider": "ChatGPT",
            },
        )


def test_existing_hash_named_upload_mismatch_fails_closed(tmp_path: Path) -> None:
    raw = _return_zip()
    digest = hashlib.sha256(raw).hexdigest()
    destination = tmp_path / ".evidenceos_delta_intake" / "research_brain" / f"gemini_{digest}.zip"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"tampered")

    with pytest.raises(ipc_worker.WorkerError, match="RETURN_PACKAGE_UPLOAD_EXISTING_HASH_MISMATCH"):
        ipc_worker._stage_public_model_return_upload(
            tmp_path,
            "Research Brain",
            {
                "package_name": "return.zip",
                "package_base64": base64.b64encode(raw).decode("ascii"),
                "provider": "Gemini",
            },
        )


def test_existing_refresh_import_route_receives_the_staged_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _return_zip()
    captured: dict[str, object] = {}

    def fake_import(workspace: Path, brain_name: str, **kwargs: object) -> dict[str, object]:
        captured.update(workspace=workspace, brain_name=brain_name, **kwargs)
        return {"status": "AWAITING_FUSE", "candidate_id": "candidate_1"}

    monkeypatch.setattr(ipc_worker, "_list_brains", lambda _workspace: [])
    monkeypatch.setattr(ipc_worker, "_sources_for", lambda *_: [])
    monkeypatch.setattr(ipc_worker, "import_public_model_return", fake_import)

    result = ipc_worker.handle(
        {
            "command": "brain.refresh.import",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": "Research Brain",
                "expected_snapshot_id": "snapshot_good",
                "package_name": "return.zip",
                "package_base64": base64.b64encode(raw).decode("ascii"),
                "provider": "ChatGPT",
            },
        }
    )

    staged = Path(str(captured["package_path"]))
    assert result == {"status": "AWAITING_FUSE", "candidate_id": "candidate_1"}
    assert captured["workspace"] == tmp_path.resolve()
    assert captured["brain_name"] == "Research Brain"
    assert captured["expected_snapshot_id"] == "snapshot_good"
    assert captured["provider"] == "ChatGPT"
    assert staged.is_file()
    assert staged.read_bytes() == raw
