from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


PIPELINE_SCHEMA_VERSION = 1
PIPELINE_HISTORY_LIMIT = 256
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
VALID_STATUSES = frozenset({"queued", "running", *TERMINAL_STATUSES})


class PipelineStateError(RuntimeError):
    """Raised when a pipeline transition or persisted state is invalid."""


class StatePersistenceError(RuntimeError):
    """Raised when an atomic JSON state file cannot be read or written."""


@dataclass(frozen=True, slots=True)
class PipelineStage:
    stage_id: str
    stage_name: str
    stage_order: int


# T021 authoritative order. Keep names synchronized with the controlling plan.
PIPELINE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage("source_validation_registration", "Source validation and registration", 1),
    PipelineStage("sqlite_project_router_sector_creation", "SQLite project/router/sector creation", 2),
    PipelineStage("per_lane_parsing_chunking_indexing", "Per-lane parsing/chunking/indexing", 3),
    PipelineStage("pointer_router_hash_finalization", "Pointer/router/hash finalization", 4),
    PipelineStage("project_mmd_generation", "Project MMD generation", 5),
    PipelineStage("svg_png_rendering", "SVG/PNG rendering", 6),
    PipelineStage("chatgpt_package_compilation", "ChatGPT package compilation", 7),
    PipelineStage("gemini_exact10_compilation", "Gemini provider-readable package compilation", 8),
    PipelineStage("package_hash_validation", "Package/hash validation", 9),
    PipelineStage("immutable_version_capture", "Immutable version capture", 10),
)
PIPELINE_STAGE_COUNT = len(PIPELINE_STAGES)
PIPELINE_STAGE_BY_ID = {stage.stage_id: stage for stage in PIPELINE_STAGES}
PIPELINE_STAGE_BY_ORDER = {stage.stage_order: stage for stage in PIPELINE_STAGES}


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    pipeline_id: str
    request_id: str
    brain_name: str
    workspace_dir: str
    stage_id: str
    stage_name: str
    stage_order: int
    stage_count: int
    stage_percent: float
    global_percent: float
    running_count: int
    queued_count: int
    completed_count: int
    active_lane: str
    active_source_id: str
    active_sector_id: str
    active_file: str
    files_done: int
    files_total: int
    rows_written: int
    chunks_written: int
    elapsed_seconds: float
    eta_seconds: float | None
    process_pid: int
    status: str
    error_code: str | None
    active_command: str
    started_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PipelineEvent":
        try:
            return cls(
                pipeline_id=str(payload["pipeline_id"]),
                request_id=str(payload.get("request_id") or ""),
                brain_name=str(payload.get("brain_name") or ""),
                workspace_dir=str(payload.get("workspace_dir") or ""),
                stage_id=str(payload["stage_id"]),
                stage_name=str(payload["stage_name"]),
                stage_order=int(payload["stage_order"]),
                stage_count=int(payload["stage_count"]),
                stage_percent=float(payload["stage_percent"]),
                global_percent=float(payload["global_percent"]),
                running_count=int(payload["running_count"]),
                queued_count=int(payload["queued_count"]),
                completed_count=int(payload["completed_count"]),
                active_lane=str(payload.get("active_lane") or ""),
                active_source_id=str(payload.get("active_source_id") or ""),
                active_sector_id=str(payload.get("active_sector_id") or ""),
                active_file=str(payload.get("active_file") or ""),
                files_done=int(payload.get("files_done") or 0),
                files_total=int(payload.get("files_total") or 0),
                rows_written=int(payload.get("rows_written") or 0),
                chunks_written=int(payload.get("chunks_written") or 0),
                elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
                eta_seconds=(
                    None if payload.get("eta_seconds") is None else float(payload["eta_seconds"])
                ),
                process_pid=int(payload["process_pid"]),
                status=str(payload["status"]),
                error_code=(None if payload.get("error_code") is None else str(payload["error_code"])),
                active_command=str(payload.get("active_command") or ""),
                started_at=str(payload["started_at"]),
                updated_at=str(payload["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineStateError(f"PIPELINE_EVENT_INVALID: {exc}") from exc


def stage_definition(stage: PipelineStage | str | int) -> PipelineStage:
    if isinstance(stage, PipelineStage):
        canonical = PIPELINE_STAGE_BY_ID.get(stage.stage_id)
    elif isinstance(stage, int):
        canonical = PIPELINE_STAGE_BY_ORDER.get(stage)
    else:
        canonical = PIPELINE_STAGE_BY_ID.get(str(stage))
    if canonical is None:
        raise PipelineStateError(f"PIPELINE_STAGE_UNKNOWN: {stage}")
    return canonical


def _utc_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _bounded_percent(value: float | int) -> float:
    return round(max(0.0, min(100.0, float(value))), 2)


def _lock_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.name}.lock")


@contextmanager
def _exclusive_state_file_lock(state_path: Path) -> Iterator[None]:
    """Use a one-byte advisory lock beside the JSON file.

    The lock complements each store's in-process ``RLock``. It protects normal
    cooperating Evidence OS processes without introducing another dependency.
    """

    lock_path = _lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + 1.5
        if os.name == "nt":  # pragma: no branch - platform-specific implementation
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise StatePersistenceError(f"STATE_BUSY: {state_path}") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on non-Windows CI
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise StatePersistenceError(f"STATE_BUSY: {state_path}") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatePersistenceError(f"STATE_FILE_READ_FAILED: {state_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StatePersistenceError(f"STATE_FILE_ROOT_NOT_OBJECT: {state_path}")
    return payload


def _atomic_write_json(state_path: Path, payload: Mapping[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{state_path.name}.", suffix=".tmp", dir=state_path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, state_path)
        temporary_name = ""
        if os.name != "nt":  # pragma: no cover - directory fsync is POSIX-specific
            directory_fd = os.open(state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise StatePersistenceError(f"STATE_FILE_WRITE_FAILED: {state_path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


class PipelineStateStore:
    """Persist and validate the authoritative T021 ten-stage pipeline event.

    ``stage_percent`` is monotonic within one stage and starts again at zero
    when the pipeline advances. ``global_percent`` and all cumulative counters
    are monotonic for the entire pipeline.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        pid_provider: Callable[[], int] = os.getpid,
        history_limit: int = PIPELINE_HISTORY_LIMIT,
    ) -> None:
        self.state_path = Path(state_path).expanduser().resolve()
        self._clock = clock
        self._pid_provider = pid_provider
        self._history_limit = max(1, int(history_limit))
        self._thread_lock = threading.RLock()

    def start(
        self,
        *,
        pipeline_id: str | None = None,
        request_id: str = "",
        brain_name: str = "",
        workspace_dir: str = "",
        process_pid: int | None = None,
        files_total: int = 0,
        active_lane: str = "",
        active_source_id: str = "",
        active_sector_id: str = "",
        active_file: str = "",
        active_command: str = "",
        replace: bool = False,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            current = _read_json_state(self.state_path)
            if current and not replace:
                event = PipelineEvent.from_mapping(current.get("event") or {})
                if event.status not in TERMINAL_STATUSES:
                    raise PipelineStateError(f"PIPELINE_ALREADY_ACTIVE: {event.pipeline_id}")

            now = float(self._clock())
            first = PIPELINE_STAGES[0]
            event = self._build_event(
                pipeline_id=pipeline_id or f"pipeline_{uuid.uuid4().hex}",
                request_id=request_id,
                brain_name=brain_name,
                workspace_dir=workspace_dir,
                stage=first,
                stage_percent=0.0,
                previous_global_percent=0.0,
                active_lane=active_lane,
                active_source_id=active_source_id,
                active_sector_id=active_sector_id,
                active_file=active_file,
                files_done=0,
                files_total=max(0, int(files_total)),
                rows_written=0,
                chunks_written=0,
                started_at_epoch=now,
                now=now,
                process_pid=int(process_pid if process_pid is not None else self._pid_provider()),
                status="running",
                error_code=None,
                active_command=active_command,
            )
            payload = {
                "schema_version": PIPELINE_SCHEMA_VERSION,
                "started_at_epoch": now,
                "event": event.to_dict(),
                "history": [event.to_dict()],
            }
            _atomic_write_json(self.state_path, payload)
            return event.to_dict()

    def update(
        self,
        *,
        stage: PipelineStage | str | int | None = None,
        stage_percent: float | int | None = None,
        active_lane: str | None = None,
        active_source_id: str | None = None,
        active_sector_id: str | None = None,
        active_file: str | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        rows_written: int | None = None,
        chunks_written: int | None = None,
        process_pid: int | None = None,
        status: str | None = None,
        error_code: str | None = None,
        active_command: str | None = None,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            payload = self._required_payload()
            previous = PipelineEvent.from_mapping(payload["event"])
            if previous.status in TERMINAL_STATUSES:
                raise PipelineStateError(f"PIPELINE_TERMINAL: {previous.status}")

            target = stage_definition(stage if stage is not None else previous.stage_id)
            if target.stage_order < previous.stage_order:
                raise PipelineStateError(
                    f"PIPELINE_STAGE_REGRESSION: {previous.stage_order}->{target.stage_order}"
                )

            requested_status = str(status or previous.status).lower()
            if requested_status not in VALID_STATUSES:
                raise PipelineStateError(f"PIPELINE_STATUS_INVALID: {requested_status}")
            if requested_status == "failed" and not (error_code or previous.error_code):
                raise PipelineStateError("PIPELINE_ERROR_CODE_REQUIRED")

            if requested_status == "completed":
                target = PIPELINE_STAGES[-1]
                next_stage_percent = 100.0
            else:
                raw_stage_percent = previous.stage_percent if stage_percent is None else stage_percent
                next_stage_percent = _bounded_percent(raw_stage_percent)
                if target.stage_order == previous.stage_order:
                    next_stage_percent = max(previous.stage_percent, next_stage_percent)

            next_files_done = max(previous.files_done, int(files_done or 0)) if files_done is not None else previous.files_done
            next_files_total = previous.files_total if files_total is None else max(0, int(files_total))
            next_files_total = max(next_files_total, next_files_done)
            next_rows = max(previous.rows_written, int(rows_written or 0)) if rows_written is not None else previous.rows_written
            next_chunks = (
                max(previous.chunks_written, int(chunks_written or 0))
                if chunks_written is not None
                else previous.chunks_written
            )
            now = float(self._clock())
            started_at_epoch = float(payload["started_at_epoch"])
            event = self._build_event(
                pipeline_id=previous.pipeline_id,
                request_id=previous.request_id,
                brain_name=previous.brain_name,
                workspace_dir=previous.workspace_dir,
                stage=target,
                stage_percent=next_stage_percent,
                previous_global_percent=previous.global_percent,
                active_lane=previous.active_lane if active_lane is None else str(active_lane),
                active_source_id=previous.active_source_id if active_source_id is None else str(active_source_id),
                active_sector_id=previous.active_sector_id if active_sector_id is None else str(active_sector_id),
                active_file=previous.active_file if active_file is None else str(active_file),
                files_done=next_files_done,
                files_total=next_files_total,
                rows_written=next_rows,
                chunks_written=next_chunks,
                started_at_epoch=started_at_epoch,
                now=now,
                process_pid=int(process_pid if process_pid is not None else previous.process_pid),
                status=requested_status,
                error_code=(error_code if error_code is not None else previous.error_code),
                active_command=previous.active_command if active_command is None else str(active_command),
            )
            history = list(payload.get("history") or [])
            history.append(event.to_dict())
            payload["event"] = event.to_dict()
            payload["history"] = history[-self._history_limit :]
            _atomic_write_json(self.state_path, payload)
            return event.to_dict()

    def complete(
        self,
        *,
        files_done: int | None = None,
        rows_written: int | None = None,
        chunks_written: int | None = None,
        active_file: str = "",
        active_command: str = "",
    ) -> dict[str, Any]:
        return self.update(
            stage=PIPELINE_STAGES[-1],
            stage_percent=100.0,
            files_done=files_done,
            rows_written=rows_written,
            chunks_written=chunks_written,
            active_file=active_file,
            active_command=active_command,
            status="completed",
            error_code=None,
        )

    def fail(self, error_code: str, *, active_file: str | None = None) -> dict[str, Any]:
        return self.update(status="failed", error_code=error_code, active_file=active_file)

    def cancel(self, error_code: str = "PIPELINE_CANCELLED") -> dict[str, Any]:
        return self.update(status="cancelled", error_code=error_code)

    def snapshot(self) -> dict[str, Any] | None:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            payload = _read_json_state(self.state_path)
            if payload is None:
                return None
            self._validate_payload(payload)
            return PipelineEvent.from_mapping(payload["event"]).to_dict()

    def history(self) -> list[dict[str, Any]]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            payload = self._required_payload()
            return [PipelineEvent.from_mapping(item).to_dict() for item in payload.get("history") or []]

    def _required_payload(self) -> dict[str, Any]:
        payload = _read_json_state(self.state_path)
        if payload is None:
            raise PipelineStateError("PIPELINE_NOT_STARTED")
        self._validate_payload(payload)
        return payload

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> None:
        if int(payload.get("schema_version") or 0) != PIPELINE_SCHEMA_VERSION:
            raise PipelineStateError("PIPELINE_SCHEMA_VERSION_UNSUPPORTED")
        if "started_at_epoch" not in payload or "event" not in payload:
            raise PipelineStateError("PIPELINE_STATE_INCOMPLETE")
        event = PipelineEvent.from_mapping(payload["event"])
        canonical = stage_definition(event.stage_id)
        if event.stage_order != canonical.stage_order or event.stage_name != canonical.stage_name:
            raise PipelineStateError("PIPELINE_STAGE_DEFINITION_MISMATCH")
        if event.stage_count != PIPELINE_STAGE_COUNT:
            raise PipelineStateError("PIPELINE_STAGE_COUNT_MISMATCH")
        if event.status not in VALID_STATUSES:
            raise PipelineStateError("PIPELINE_STATUS_INVALID")

    @staticmethod
    def _build_event(
        *,
        pipeline_id: str,
        request_id: str,
        brain_name: str,
        workspace_dir: str,
        stage: PipelineStage,
        stage_percent: float,
        previous_global_percent: float,
        active_lane: str,
        active_source_id: str,
        active_sector_id: str,
        active_file: str,
        files_done: int,
        files_total: int,
        rows_written: int,
        chunks_written: int,
        started_at_epoch: float,
        now: float,
        process_pid: int,
        status: str,
        error_code: str | None,
        active_command: str,
    ) -> PipelineEvent:
        stage_percent = _bounded_percent(stage_percent)
        calculated_global = (
            ((stage.stage_order - 1) + (stage_percent / 100.0)) / PIPELINE_STAGE_COUNT
        ) * 100.0
        global_percent = max(_bounded_percent(previous_global_percent), _bounded_percent(calculated_global))
        if status == "completed":
            stage_percent = 100.0
            global_percent = 100.0

        elapsed = round(max(0.0, now - started_at_epoch), 3)
        if status == "completed":
            eta: float | None = 0.0
        elif status != "running" or global_percent <= 0.0:
            eta = None
        else:
            eta = round(elapsed * (100.0 - global_percent) / global_percent, 3)

        if status == "completed":
            running_count, queued_count, completed_count = 0, 0, PIPELINE_STAGE_COUNT
        elif status == "running":
            running_count = 1
            queued_count = PIPELINE_STAGE_COUNT - stage.stage_order
            completed_count = stage.stage_order - 1
        elif status == "queued":
            running_count = 0
            queued_count = PIPELINE_STAGE_COUNT - stage.stage_order + 1
            completed_count = stage.stage_order - 1
        else:
            running_count = 0
            queued_count = PIPELINE_STAGE_COUNT - stage.stage_order
            completed_count = stage.stage_order - 1

        return PipelineEvent(
            pipeline_id=str(pipeline_id),
            request_id=str(request_id or ""),
            brain_name=str(brain_name or ""),
            workspace_dir=str(workspace_dir or ""),
            stage_id=stage.stage_id,
            stage_name=stage.stage_name,
            stage_order=stage.stage_order,
            stage_count=PIPELINE_STAGE_COUNT,
            stage_percent=stage_percent,
            global_percent=global_percent,
            running_count=running_count,
            queued_count=queued_count,
            completed_count=completed_count,
            active_lane=str(active_lane or ""),
            active_source_id=str(active_source_id or ""),
            active_sector_id=str(active_sector_id or ""),
            active_file=str(active_file or ""),
            files_done=max(0, int(files_done)),
            files_total=max(max(0, int(files_total)), max(0, int(files_done))),
            rows_written=max(0, int(rows_written)),
            chunks_written=max(0, int(chunks_written)),
            elapsed_seconds=elapsed,
            eta_seconds=eta,
            process_pid=max(0, int(process_pid)),
            status=status,
            error_code=(str(error_code) if error_code else None),
            active_command=str(active_command or ""),
            started_at=_utc_iso(started_at_epoch),
            updated_at=_utc_iso(now),
        )
