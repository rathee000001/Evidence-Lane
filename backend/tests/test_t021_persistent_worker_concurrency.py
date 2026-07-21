from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlite_brain_builder import ipc_worker


def _request(request_id: str, command: str, workspace: Path, brain: str = "Brain One") -> dict[str, object]:
    return {
        "id": request_id,
        "command": command,
        "payload": {
            "workspace_dir": str(workspace),
            "brain_name": brain,
            "tauri_pid": os.getpid(),
        },
    }


def test_runtime_snapshot_is_observational_and_creates_no_runtime_state(tmp_path: Path) -> None:
    response = ipc_worker.handle(_request("snapshot", "runtime.snapshot", tmp_path))

    assert response["pipeline"]["pipeline"] is None
    assert not (tmp_path / ".evidenceos_runtime").exists()
    assert not (tmp_path / "Brain One").exists()


def test_read_only_metadata_request_does_not_create_process_runtime_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ipc_worker._serve_request(json.dumps(_request("lanes", "lanes.defs", tmp_path))) == 0
    capsys.readouterr()

    assert not (tmp_path / ".evidenceos_runtime").exists()


def test_worker_accepts_multiple_requests_in_one_process(tmp_path: Path) -> None:
    requests = [
        _request("one", "lanes.defs", tmp_path),
        _request("two", "runtime.snapshot", tmp_path),
    ]
    environment = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "sqlite_brain_builder.ipc_worker"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )
    responses = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip() and json.loads(line).get("type") == "response"
    ]

    assert completed.returncode == 0, completed.stderr
    assert [response["id"] for response in responses] == ["one", "two"]
    assert all(response["ok"] for response in responses)


def test_mutation_command_registry_covers_every_persistent_state_command() -> None:
    expected = {
        "workspace.init",
        "workspace.outputRoot.choose",
        "workspace.rootHistory.add",
        "workspace.rootHistory.drop",
        "local.workspace.init",
        "profile.update",
        "settings.update",
        "settings.reset",
        "settings.import",
        "admin.settings.update",
        "credential.set",
        "credential.delete",
        "model.connector.upsert",
        "model.connector.setActive",
        "model.connector.disable",
        "model.connector.discoverModels",
        "model.connector.validate",
        "model.connector.delete",
        "model.connector.execute",
        "ollama.settings.update",
        "ollama.registry.refresh",
        "ollama.launch",
        "codex.launch",
        "project.create",
        "chat.create",
        "chat.rename",
        "chat.pin",
        "chat.unpin",
        "chat.move",
        "chat.detach",
        "chat.archive",
        "chat.unarchive",
        "chat.markRead",
        "chat.delete",
        "message.append",
        "attachment.link",
        "session.update",
        "chat.lineage.append",
        "brain.create",
        "brain.select",
        "brain.selection.timing.record",
        "brain.rename",
        "brain.pin",
        "brain.unpin",
        "brain.remove",
        "brain.deleteToRecycleBin",
        "brains.discover",
        "brain.version.capture",
        "brain.snapshot.markGood",
        "brain.planDelta.state",
        "brain.planDelta.append",
        "brain.planDelta.disposition",
        "brain.planGoalPrompt.read",
        "brain.planStateSlip.append",
        "brain.refresh.start",
        "brain.refresh.import",
        "brain.refresh.hil.decide",
        "brain.refresh.cancel",
        "brain.refresh.retry",
        "brain.refresh.fuse",
        "brain.refresh.drop",
        "brain.refresh.cleanup",
        "brain.review.acceptByContinuation",
        "brain.telemetry.openTarget",
        "brain.codexHandoff.create",
        "brain.portablePackage.create",
        "brain.portablePackage.query",
        "brain.codexLedger.task.append",
        "brain.codexLedger.usage.record",
        "brain.codexLedger.rq.record",
        "brain.codexLedger.steerPrompt.record",
        "brain.modelLedger.execution.append",
        "brain.modelLedger.endpointEvent.append",
        "brain.modelLedger.failure.append",
        "brain.portablePackage.export",
        "brain.version.rollback",
        "brain.version.drop",
        "sources.add",
        "sources.cloneGithub",
        "sources.remove",
        "sources.removeLane",
        "source.schema.update",
        "source.schema.reset",
        "flash.read",
        "brain.build",
        "brain.buildAll",
        "topology.render",
        "package.exportChatGPT",
        "package.exportGemini",
    }

    assert ipc_worker._MUTATING_COMMANDS == expected
    assert "runtime.snapshot" not in ipc_worker._MUTATING_COMMANDS
    assert "brain.portablePackage.validate" not in ipc_worker._MUTATING_COMMANDS


@pytest.mark.parametrize("command", sorted(ipc_worker._MUTATING_COMMANDS))
def test_mutation_coordinator_returns_stable_busy_for_every_command_family(
    tmp_path: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ipc_worker, "_MUTATION_LOCK_TIMEOUT_SECONDS", 0.05)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with ipc_worker._mutation_coordinator(tmp_path, "Brain One", command):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(ipc_worker.WorkerError, match=r"^BUSY:"):
            with ipc_worker._mutation_coordinator(tmp_path, "Brain One", command):
                pass
    finally:
        release.set()
        thread.join(timeout=2)


def test_coordination_files_are_outside_renameable_brain_folders(tmp_path: Path) -> None:
    runtime = ipc_worker._coordination_root(tmp_path)
    brain_output = ipc_worker.brain_output_dir(tmp_path, "Brain One")

    assert runtime.is_relative_to(tmp_path / ".evidenceos_runtime")
    assert not runtime.is_relative_to(brain_output)


def test_dead_process_mutation_lock_is_recovered_without_touching_live_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead_pid = 999_999_991

    class NoSuchProcess(Exception):
        pass

    class Process:
        def __init__(self, pid: int) -> None:
            if pid == dead_pid:
                raise NoSuchProcess(pid)

        def create_time(self) -> float:
            return 1234.5

    monkeypatch.setattr(
        ipc_worker,
        "_psutil",
        SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcess),
    )
    runtime_key = ipc_worker._brain_runtime_key(tmp_path, "Brain One")
    lock_path = ipc_worker._coordination_root(tmp_path) / "locks" / f"{runtime_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{dead_pid}|10.0|brain.buildAll|1", encoding="utf-8")

    with ipc_worker._mutation_coordinator(tmp_path, "Brain One", "brain.buildAll"):
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()}|1234.5|")

    assert not lock_path.exists()


def test_identical_worker_failures_are_coalesced(tmp_path: Path) -> None:
    request = _request("failure-one", "brain.buildAll", tmp_path)
    first = ipc_worker._write_worker_failure_report(request, RuntimeError("same failure"))
    request["id"] = "failure-two"
    second = ipc_worker._write_worker_failure_report(request, RuntimeError("same failure"))

    assert first == second
    assert first is not None and first.exists()
    assert len(list(first.parent.glob("worker_error_*.json"))) == 1


def test_snapshot_failures_never_create_receipts(tmp_path: Path) -> None:
    report = ipc_worker._write_worker_failure_report(
        _request("snapshot-failure", "runtime.snapshot", tmp_path),
        RuntimeError("transient snapshot failure"),
    )

    assert report is None
    assert not (tmp_path / ".evidenceos_runtime").exists()
