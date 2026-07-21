from __future__ import annotations

import base64
import json
import re
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlite_brain_builder.brain_versions import list_brain_versions
from sqlite_brain_builder.core import now
from sqlite_brain_builder.runtime.env15_chat_lineage import append_env15_chat_lineage_source
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir, slugify_name
from sqlite_brain_builder.runtime.plan_delta_governance import record_model_state_slip
from sqlite_brain_builder.runtime.portable_brain_package import (
    RQ_QUESTIONS,
    append_endpoint_execution_event,
    append_model_execution_run,
    append_model_failure_event,
    append_task_run,
    initialize_operational_ledger,
    record_model_usage,
    record_project_query,
    record_rq_answers,
    record_steer_prompt_answer,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import ingest_env15_structural_source
from sqlite_brain_builder.workspace.local_workspace import (
    DEFAULT_IDENTITY_ID,
    ensure_local_workspace,
    read_protected_credential,
)
from sqlite_brain_builder.workspace.model_connectors import (
    CUSTOM_PROMPT_BAR_TYPES,
    ModelConnectorError,
    get_model_connector,
)
from sqlite_brain_builder.workspace.selected_brain_state import (
    SelectedBrainStateError,
    materialize_model_output,
)


class ModelExecutionError(RuntimeError):
    pass


Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]

_MAX_PROMPT_CHARS = 64 * 1024
_MAX_EVIDENCE_SLICES = 32
_MAX_EVIDENCE_SLICE_CHARS = 16 * 1024
_MAX_EVIDENCE_TOTAL_CHARS = 256 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOKEN_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
}
_SECRET_VALUE = re.compile(
    r"(?:^|\s)(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9]{12,}|xox[baprs]-\S+|AIza[0-9A-Za-z_-]{12,})",
    re.IGNORECASE,
)


@dataclass
class _ExecutionFailure(Exception):
    code: str
    failure_class: str
    retry_safe: bool
    next_action_code: str
    next_action_text: str
    unresolved_risk: str
    observed_evidence: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise HTTPError(req.full_url, code, "MODEL_ENDPOINT_REDIRECT_FORBIDDEN", headers, fp)


def _ledger_path(workspace: Path, brain_name: str) -> Path:
    return workspace / "portable_brain_workspaces" / slugify_name(brain_name) / "codex" / "codex_runtime_ledger.sqlite"


def _bounded_prompt(value: str) -> str:
    prompt = str(value or "").strip()
    if not prompt:
        raise _ExecutionFailure(
            "MODEL_EXECUTION_PROMPT_REQUIRED",
            "PAYLOAD_REJECTED",
            True,
            "ENTER_BOUNDED_PROMPT",
            "Enter a bounded prompt and retry.",
            "No endpoint request was sent.",
        )
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise _ExecutionFailure(
            "MODEL_EXECUTION_PROMPT_TOO_LARGE",
            "PAYLOAD_REJECTED",
            True,
            "REDUCE_PROMPT",
            "Reduce the prompt below the bounded request limit.",
            "No endpoint request was sent.",
            {"prompt_characters": len(prompt), "maximum_characters": _MAX_PROMPT_CHARS},
        )
    if _SECRET_VALUE.search(prompt):
        raise _ExecutionFailure(
            "MODEL_EXECUTION_PROMPT_CONTAINS_SECRET_LIKE_VALUE",
            "PAYLOAD_REJECTED",
            True,
            "REMOVE_SECRET_FROM_PROMPT",
            "Remove secret material and use an OS-protected credential reference.",
            "The prompt was not logged or sent.",
        )
    return prompt


def _bounded_evidence(rows: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    if rows is None:
        return []
    if isinstance(rows, (str, bytes)) or len(rows) > _MAX_EVIDENCE_SLICES:
        raise _ExecutionFailure(
            "MODEL_EXECUTION_EVIDENCE_SLICE_LIMIT_EXCEEDED",
            "PAYLOAD_REJECTED",
            True,
            "REDUCE_EVIDENCE_SLICE",
            "Provide fewer bounded evidence slices.",
            "No endpoint request was sent.",
            {"maximum_slices": _MAX_EVIDENCE_SLICES},
        )
    bounded: list[dict[str, str]] = []
    total = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise _ExecutionFailure(
                "MODEL_EXECUTION_EVIDENCE_SLICE_INVALID",
                "PAYLOAD_REJECTED",
                True,
                "CORRECT_EVIDENCE_SLICE",
                "Each bounded evidence slice must contain an ID and content.",
                "No endpoint request was sent.",
                {"slice_index": index},
            )
        slice_id = str(row.get("id") or "").strip()
        content = str(row.get("content") or "").strip()
        if not slice_id or not content or len(content) > _MAX_EVIDENCE_SLICE_CHARS:
            raise _ExecutionFailure(
                "MODEL_EXECUTION_EVIDENCE_SLICE_INVALID",
                "PAYLOAD_REJECTED",
                True,
                "CORRECT_EVIDENCE_SLICE",
                "Each bounded evidence slice must contain a valid ID and bounded content.",
                "No endpoint request was sent.",
                {"slice_index": index, "maximum_slice_characters": _MAX_EVIDENCE_SLICE_CHARS},
            )
        total += len(content)
        bounded.append({"id": slice_id, "content": content})
    if total > _MAX_EVIDENCE_TOTAL_CHARS:
        raise _ExecutionFailure(
            "MODEL_EXECUTION_EVIDENCE_TOTAL_LIMIT_EXCEEDED",
            "PAYLOAD_REJECTED",
            True,
            "REDUCE_EVIDENCE_SLICE",
            "Reduce the total bounded evidence size.",
            "No endpoint request was sent.",
            {"evidence_characters": total, "maximum_characters": _MAX_EVIDENCE_TOTAL_CHARS},
        )
    return bounded


def _safe_request_url(profile: Mapping[str, Any]) -> str:
    base = urlsplit(str(profile.get("base_url") or ""))
    path = str(profile.get("request_path") or "").strip()
    request = urlsplit(path)
    if base.scheme not in {"http", "https"} or not base.netloc or base.username or base.password:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_BASE_URL_INVALID",
            "PAYLOAD_REJECTED",
            False,
            "EDIT_ENDPOINT_PROFILE",
            "Correct the endpoint base URL in Settings.",
            "The endpoint target is not safe to call.",
        )
    if not path.startswith("/") or request.scheme or request.netloc or request.query or request.fragment:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_REQUEST_PATH_INVALID",
            "PAYLOAD_REJECTED",
            False,
            "EDIT_ENDPOINT_PROFILE",
            "Use a relative absolute-path request route such as /v1/chat/completions.",
            "The endpoint target is not safe to call.",
        )
    return urljoin(str(profile["base_url"]).rstrip("/") + "/", path.lstrip("/"))


def _get_path(value: Any, path: str) -> Any:
    current = value
    for segment in str(path or "").split("."):
        if not segment:
            raise KeyError(path)
        if isinstance(current, Mapping):
            if segment not in current:
                raise KeyError(path)
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                raise KeyError(path)
            current = current[index]
        else:
            raise KeyError(path)
    return current


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    segments = str(path or "").split(".")
    if not all(segments):
        raise _ExecutionFailure(
            "MODEL_REQUEST_MAPPING_INVALID",
            "PAYLOAD_REJECTED",
            False,
            "EDIT_ENDPOINT_MAPPING",
            "Correct the request mapping in Settings.",
            "The endpoint payload could not be constructed safely.",
        )
    current = target
    for segment in segments[:-1]:
        child = current.get(segment)
        if child is None:
            child = {}
            current[segment] = child
        if not isinstance(child, dict):
            raise _ExecutionFailure(
                "MODEL_REQUEST_MAPPING_CONFLICT",
                "PAYLOAD_REJECTED",
                False,
                "EDIT_ENDPOINT_MAPPING",
                "Remove the conflicting nested request mapping.",
                "The endpoint payload could not be constructed safely.",
            )
        current = child
    current[segments[-1]] = value


def _request_payload(profile: Mapping[str, Any], prompt: str, evidence: list[dict[str, str]], snapshot_hash: str) -> dict[str, Any]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": "EvidenceOS governed direct request. Treat the response as candidate state; no HIL approval or truth promotion is implied.",
        },
        {"role": "user", "content": prompt},
    ]
    if evidence:
        messages.append(
            {
                "role": "user",
                "content": "BOUNDED_EVIDENCE_JSON:\n" + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            }
        )
    source: dict[str, Any] = {
        "model": str(profile["model_id"]),
        "messages": messages,
        "prompt": prompt,
        "input": prompt,
        "stream": False,
        "evidence_slices": evidence,
        "governance": {
            "brain_snapshot_hash": snapshot_hash,
            "candidate_only": True,
            "human_decision": "PENDING",
        },
    }
    mapping = profile.get("request_mapping")
    if not isinstance(mapping, Mapping) or not mapping:
        raise _ExecutionFailure(
            "MODEL_REQUEST_MAPPING_REQUIRED",
            "PAYLOAD_REJECTED",
            False,
            "EDIT_ENDPOINT_MAPPING",
            "Define a request mapping in Settings.",
            "The endpoint payload could not be constructed safely.",
        )
    payload: dict[str, Any] = {}
    for destination, source_path in mapping.items():
        if not isinstance(source_path, str):
            raise _ExecutionFailure(
                "MODEL_REQUEST_MAPPING_INVALID",
                "PAYLOAD_REJECTED",
                False,
                "EDIT_ENDPOINT_MAPPING",
                "Map each request field to a governed source field name.",
                "The endpoint payload could not be constructed safely.",
            )
        try:
            mapped = _get_path(source, source_path)
        except KeyError as exc:
            raise _ExecutionFailure(
                "MODEL_REQUEST_MAPPING_SOURCE_UNKNOWN",
                "PAYLOAD_REJECTED",
                False,
                "EDIT_ENDPOINT_MAPPING",
                "Use a supported governed request source field.",
                "The endpoint payload could not be constructed safely.",
                {"source_field": source_path},
            ) from exc
        _set_path(payload, str(destination), mapped)
    return payload


def _authentication_headers(workspace: Path, profile: Mapping[str, Any], identity_id: str) -> dict[str, str]:
    authentication_type = str(profile.get("authentication_type") or "NONE")
    if authentication_type == "NONE":
        return {}
    reference = str(profile.get("authentication_reference") or "").strip()
    if not reference:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_AUTHENTICATION_MISSING",
            "AUTHENTICATION_MISSING",
            True,
            "CONFIGURE_PROTECTED_CREDENTIAL",
            "Save an OS-protected credential in endpoint Settings.",
            "The endpoint request was not sent.",
        )
    try:
        secret = str(read_protected_credential(workspace, reference, identity_id)["secret"])
    except Exception as exc:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_AUTHENTICATION_MISSING",
            "AUTHENTICATION_MISSING",
            True,
            "REPAIR_PROTECTED_CREDENTIAL",
            "Repair or replace the OS-protected credential reference.",
            "The endpoint request was not sent.",
        ) from exc
    if authentication_type == "BEARER_REFERENCE":
        return {"Authorization": f"Bearer {secret}"}
    if authentication_type == "API_KEY_REFERENCE":
        return {"X-API-Key": secret}
    if authentication_type == "BASIC_REFERENCE":
        token = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    if authentication_type == "CUSTOM_REFERENCE":
        return {"Authorization": secret}
    raise _ExecutionFailure(
        "MODEL_ENDPOINT_AUTHENTICATION_TYPE_UNSUPPORTED",
        "AUTHENTICATION_MISSING",
        False,
        "EDIT_ENDPOINT_PROFILE",
        "Choose a supported authentication type.",
        "The endpoint request was not sent.",
    )


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "EvidenceOS-T023/1", **headers},
        method="POST",
    )
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_RESPONSE_TOO_LARGE",
            "MODEL_RESPONSE_INVALID",
            False,
            "REVIEW_ENDPOINT_RESPONSE_LIMIT",
            "Configure the endpoint to return a bounded response.",
            "The oversized response was not retained.",
            {"maximum_response_bytes": _MAX_RESPONSE_BYTES},
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_RESPONSE_INVALID_JSON",
            "MODEL_RESPONSE_INVALID",
            True,
            "RETRY_OR_EDIT_RESPONSE_MAPPING",
            "Retry or correct the endpoint response contract.",
            "The response could not be classified.",
        ) from exc
    if not isinstance(value, dict):
        raise _ExecutionFailure(
            "MODEL_ENDPOINT_RESPONSE_OBJECT_REQUIRED",
            "MODEL_RESPONSE_INVALID",
            True,
            "RETRY_OR_EDIT_RESPONSE_MAPPING",
            "Return a JSON object or correct the endpoint adapter.",
            "The response could not be classified.",
        )
    return value


def _response_fields(
    profile: Mapping[str, Any],
    response: Mapping[str, Any],
) -> tuple[str, str | None, dict[str, int], str, Mapping[str, Any] | None]:
    response_mapping = profile.get("response_mapping")
    if not isinstance(response_mapping, Mapping) or not response_mapping.get("text"):
        raise _ExecutionFailure(
            "MODEL_RESPONSE_TEXT_MAPPING_REQUIRED",
            "MODEL_RESPONSE_INVALID",
            False,
            "EDIT_RESPONSE_MAPPING",
            "Map the response text field in Settings.",
            "The response cannot be shown or promoted.",
        )
    try:
        text = _get_path(response, str(response_mapping["text"]))
    except KeyError as exc:
        raise _ExecutionFailure(
            "MODEL_RESPONSE_TEXT_MISSING",
            "MODEL_RESPONSE_INVALID",
            True,
            "RETRY_OR_EDIT_RESPONSE_MAPPING",
            "Retry or correct the response text mapping.",
            "The response cannot be shown or promoted.",
        ) from exc
    if not isinstance(text, str) or not text.strip():
        raise _ExecutionFailure(
            "MODEL_RESPONSE_TEXT_INVALID",
            "MODEL_RESPONSE_INVALID",
            True,
            "RETRY_OR_EDIT_RESPONSE_MAPPING",
            "Retry or correct the response text mapping.",
            "The response cannot be shown or promoted.",
        )
    provider_response_id: str | None = None
    provider_path = response_mapping.get("provider_response_id")
    if provider_path:
        try:
            raw_id = _get_path(response, str(provider_path))
            provider_response_id = str(raw_id) if raw_id not in (None, "") else None
        except KeyError:
            provider_response_id = None

    resulting_output: Mapping[str, Any] | None = None
    resulting_output_path = response_mapping.get("output") or response_mapping.get("resulting_delta")
    if resulting_output_path:
        try:
            raw_output = _get_path(response, str(resulting_output_path))
        except KeyError as exc:
            raise _ExecutionFailure(
                "MODEL_OUTPUT_MISSING",
                "MODEL_RESPONSE_INVALID",
                True,
                "RETRY_OR_EDIT_RESPONSE_MAPPING",
                "Return the governed output or remove its response mapping.",
                "The response is visible but no output can be indexed.",
            ) from exc
        if not isinstance(raw_output, Mapping):
            raise _ExecutionFailure(
                "MODEL_OUTPUT_OBJECT_REQUIRED",
                "MODEL_RESPONSE_INVALID",
                True,
                "RETRY_OR_EDIT_RESPONSE_MAPPING",
                "Return a governed output object with content and SHA-256.",
                "A proposed output is lineage evidence, not project truth.",
            )
        resulting_output = raw_output

    usage: dict[str, int] = {}
    usage_mapping = profile.get("usage_mapping")
    if isinstance(usage_mapping, Mapping):
        for field_name, source_path in usage_mapping.items():
            if field_name not in _TOKEN_FIELDS or not isinstance(source_path, str):
                continue
            try:
                value = _get_path(response, source_path)
            except KeyError:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _ExecutionFailure(
                    "MODEL_RESPONSE_USAGE_INVALID",
                    "MODEL_RESPONSE_INVALID",
                    True,
                    "RETRY_OR_EDIT_USAGE_MAPPING",
                    "Retry or correct the authoritative usage mapping.",
                    "Token usage cannot be recorded as exact.",
                    {"usage_field": str(field_name)},
                )
            usage[str(field_name)] = value
    classification = "EXACT_PROVIDER_USAGE" if usage else "UNAVAILABLE_AUTHORITATIVE_USAGE"
    return text.strip(), provider_response_id, usage, classification, resulting_output


def _append_governed_prompt_receipts(
    ledger: Path,
    *,
    task_id: str,
    execution_id: str,
    snapshot_hash: str,
    prompt: str,
    response_text: str,
    endpoint_id: str,
    model_id: str,
    provider_response_id: str | None,
    usage: Mapping[str, int],
    usage_classification: str,
    start_timestamp: str,
    output_sha256: str | None,
) -> int:
    end_timestamp = now()
    task_run_id = append_task_run(
        ledger,
        {
            "parent_goal_id": "EVIDENCEOS_GOVERNED_MODEL_EXECUTION",
            "run_id": execution_id,
            "task_id": task_id,
            "step_id": "model.connector.execute",
            "delta_ids_active": [],
            "model": model_id,
            "reasoning_mode": "BOUNDED_DIRECT_REQUEST",
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "starting_snapshot_hash": snapshot_hash,
            "ending_candidate_hash": output_sha256,
            "sources_queried": [],
            "sqlite_tables_queried": [],
            "fts_queries_issued": [],
            "raw_files_reread": [],
            "unchanged_files_skipped": [],
            "files_changed": [],
            "tests_run": [],
            "test_results": [],
            "build_status": "NOT_RUN",
            "human_gate_status": "PENDING",
            "completion_status": "CANDIDATE_RESPONSE",
            "next_exact_pointer": "EXPLICIT_USER_REFRESH_OR_REVIEW_REQUIRED",
        },
    )
    record_project_query(
        ledger,
        {
            "task_run_id": task_run_id,
            "snapshot_hash": snapshot_hash,
            "query_type": "MODEL_PROMPT",
            "query_text": prompt,
            "tables_queried": [],
            "result_count": 0,
        },
    )
    usage_record: dict[str, Any] = {
        "task_run_id": task_run_id,
        "goal_id": "EVIDENCEOS_GOVERNED_MODEL_EXECUTION",
        "run_id": execution_id,
        "task_id": task_id,
        "classification": usage_classification,
        "usage_source": "AUTHORITATIVE_PROVIDER_RESPONSE" if usage else "PROVIDER_RESPONSE_WITHOUT_AUTHORITATIVE_USAGE",
        "provider": endpoint_id,
        "model": model_id,
        "provider_response_id": provider_response_id,
        "call_started_at": start_timestamp,
        "call_completed_at": end_timestamp,
    }
    if usage_classification == "EXACT_PROVIDER_USAGE":
        usage_record.update({key: value for key, value in usage.items() if key in _TOKEN_FIELDS})
    record_model_usage(ledger, usage_record)
    unavailable = usage_classification != "EXACT_PROVIDER_USAGE"
    answers = {
        question_id: (
            "UNAVAILABLE_AUTHORITATIVE_USAGE; no token counts were estimated."
            if question_id in {"Q-09", "Q-10"} and unavailable
            else "Exact provider usage was appended from the authoritative response object."
            if question_id == "Q-09"
            else "A human decision remains required; no candidate was refreshed or fused automatically."
            if question_id == "Q-11"
            else "Continue only by an explicit governed user command from the selected-brain context."
            if question_id == "Q-12"
            else "Bounded model execution receipt recorded; this row remains CANDIDATE and does not promote truth."
        )
        for question_id in RQ_QUESTIONS
    }
    record_rq_answers(ledger, task_run_id, answers)
    record_steer_prompt_answer(
        ledger,
        {
            "task_run_id": task_run_id,
            "steer_id": task_id,
            "prompt_id": execution_id,
            "prompt_text": prompt,
            "answer_text": response_text,
            "answer_status": "CANDIDATE",
            "usage_classification": usage_classification,
            "total_tokens": _authoritative_total_tokens(usage)
            if usage_classification == "EXACT_PROVIDER_USAGE"
            else None,
            "call_started_at": start_timestamp,
            "call_completed_at": end_timestamp,
        },
    )
    return task_run_id


def _authoritative_total_tokens(usage: Mapping[str, int]) -> int | None:
    if "total_tokens" in usage:
        return int(usage["total_tokens"])
    if "input_tokens" in usage and "output_tokens" in usage:
        return int(usage["input_tokens"]) + int(usage["output_tokens"])
    return None


def _model_family(profile: Mapping[str, Any]) -> str:
    endpoint_type = str(profile.get("endpoint_type") or "CUSTOM_API").strip().upper()
    if endpoint_type == "OLLAMA_NATIVE":
        return "LOCAL_AI"
    if endpoint_type in {"OPENAI_COMPATIBLE", "OPENAI_API"}:
        return "PUBLIC_OR_COMPATIBLE_API"
    return endpoint_type or "CUSTOM_API"


def _append_model_state_travel(
    brain_root: Path,
    *,
    execution_id: str,
    endpoint_id: str,
    model_family: str,
    model_name: str,
    snapshot_hash: str,
    prompt: str,
    response_text: str,
    provider_response_id: str | None,
    usage: Mapping[str, int],
    output_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_directory = brain_root / "receipts" / "model_execution" / "research_sources"
    source_directory.mkdir(parents=True, exist_ok=True)
    source_path = source_directory / f"{execution_id}.md"
    research_text = (
        f"Research Question: {prompt}\n\n"
        f"Finding: {response_text}\n\n"
        f"Evidence: endpoint={endpoint_id}; model={model_name}; "
        f"provider_response_id={provider_response_id or 'unavailable'}; "
        f"brain_snapshot_sha256={snapshot_hash}; candidate_only=true.\n\n"
        "Limitation: This public or custom model turn remains candidate state. "
        "It is not fused or promoted without the governed Refresh/Fuse and HIL path.\n"
    )
    source_path.write_text(research_text, encoding="utf-8")
    try:
        research = ingest_env15_structural_source(
            brain_root,
            "research",
            {
                "source_id": f"model_execution_research_{execution_id}",
                "lane_key": "research",
                "display_name": f"Model execution research {execution_id}",
                "path": str(source_path),
                "active": True,
            },
        )
        attachments = []
        if output_record:
            attachments.append(
                {
                    "path": str(output_record.get("output_path") or ""),
                    "display_name": str(output_record.get("output_description") or output_record.get("output_id") or "Model output"),
                    "original_name": Path(str(output_record.get("output_path") or "model-output.txt")).name,
                    "stable_file_id": str(output_record.get("output_id") or ""),
                    "external_display_uri": str(output_record.get("displayed_link") or output_record.get("output_path") or ""),
                    "mime_type": str(output_record.get("media_type") or "text/plain"),
                    "size_bytes": output_record.get("size_bytes"),
                    "sha256": str(output_record.get("output_sha256") or ""),
                    "direction": "OUTPUT",
                    "availability_state": "AVAILABLE",
                    "ephemeral": False,
                }
            )
        lineage = append_env15_chat_lineage_source(
            brain_root,
            {
                "source_id": f"model_execution_lineage_{execution_id}",
                "text": prompt,
                "assistant_response": response_text,
                "visible_reasoning_summary": "VISIBLE_REASONING_NOT_EXPOSED_BY_PROVIDER",
                "turn_id": f"MODEL.{execution_id}",
                "idempotency_key": f"model_execution:{execution_id}:{snapshot_hash}",
                "provider": endpoint_id,
                "model": model_name,
                "source_classification": "MODEL_EXECUTION_STATE_TRAVEL",
                "task_window_id": execution_id,
                "attachments": attachments,
            },
        )
        exit_slip = record_model_state_slip(
            brain_root,
            slip_kind="EXIT",
            model_family=model_family,
            model_name=model_name,
            prompt_index=1,
            gates=["ENV15", "MODEL_ENDPOINT", "STATE_TRAVEL", "HIL_PENDING"],
            lanes=["research", "chat_lineage"],
            turn_id=f"{execution_id}:EXIT",
            token_count=_authoritative_total_tokens(usage),
        )
    finally:
        if source_path.exists():
            source_path.chmod(
                source_path.stat().st_mode
                & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            )
    return {
        "status": "PASS",
        "classification": "PUBLIC_MODEL_CANDIDATE_STATE_TRAVEL",
        "research": {
            "source_id": str(getattr(research, "source_id", "")),
            "source_sha256": str(getattr(research, "source_sha256", "")),
            "status": str(getattr(research, "status", "")),
            "source_path": str(source_path),
        },
        "chat_lineage": lineage,
        "outputs": [dict(output_record)] if output_record else [],
        "entry_exit": {
            "exit": exit_slip,
            "token_source": (
                "AUTHORITATIVE_PROVIDER_USAGE"
                if _authoritative_total_tokens(usage) is not None
                else "UNAVAILABLE_AUTHORITATIVE_USAGE"
            ),
        },
        "candidate_fused": False,
        "human_decision": "PENDING",
    }


def _classify_transport_error(exc: Exception) -> _ExecutionFailure:
    if isinstance(exc, TimeoutError):
        return _ExecutionFailure(
            "REQUEST_TIMEOUT",
            "REQUEST_TIMEOUT",
            True,
            "SAFE_RETRY",
            "Retry the preserved request after checking endpoint availability.",
            "Endpoint availability remains unknown.",
        )
    if isinstance(exc, HTTPError):
        status = int(exc.code)
        if status in {401, 403}:
            return _ExecutionFailure(
                "AUTHENTICATION_REJECTED",
                "AUTHENTICATION_REJECTED",
                True,
                "REPAIR_PROTECTED_CREDENTIAL",
                "Verify the protected credential and endpoint access policy.",
                "The endpoint rejected authentication.",
                {"http_status": status},
            )
        return _ExecutionFailure(
            "ENDPOINT_HTTP_ERROR",
            "ENDPOINT_UNREACHABLE",
            True,
            "SAFE_RETRY",
            "Review endpoint status and retry the preserved request.",
            "The endpoint did not accept the request.",
            {"http_status": status},
        )
    if isinstance(exc, (URLError, OSError)):
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return _classify_transport_error(TimeoutError())
        return _ExecutionFailure(
            "ENDPOINT_UNREACHABLE",
            "ENDPOINT_UNREACHABLE",
            True,
            "SAFE_RETRY",
            "Check the configured endpoint and retry the preserved request.",
            "Endpoint reachability remains unknown.",
        )
    return _ExecutionFailure(
        "UNKNOWN_FAILURE",
        "UNKNOWN_FAILURE",
        False,
        "REVIEW_LOCAL_FAILURE_RECEIPT",
        "Review the local redacted failure receipt before retrying.",
        "The exact cause is not established.",
        {"exception_type": type(exc).__name__},
    )


def _append_failure(
    ledger: Path,
    failure: _ExecutionFailure,
    *,
    execution_id: str,
    execution_run_id: int | None,
    endpoint_id: str,
    model_id: str,
    task_id: str,
) -> None:
    if execution_run_id is not None:
        append_endpoint_execution_event(
            ledger,
            {
                "execution_run_id": execution_run_id,
                "event_type": "REQUEST_FAILED",
                "status": "FAIL",
                "usage_classification": "UNAVAILABLE",
                "error_code": failure.code,
                "exact_error": failure.code,
            },
        )
    append_model_failure_event(
        ledger,
        {
            "failure_id": f"failure_{uuid.uuid4().hex}",
            "execution_run_id": execution_run_id,
            "task_id": task_id,
            "model_or_endpoint": f"{endpoint_id}/{model_id or 'UNRESOLVED'}",
            "package_id": None,
            "lane_id": "direct_model_endpoint",
            "stage": "model.connector.execute",
            "failure_class": failure.failure_class,
            "observed_evidence": failure.observed_evidence,
            "exact_error": failure.code,
            "inferred_cause": None,
            "inferred_cause_labeled": False,
            "retry_safe": failure.retry_safe,
            "data_preserved": True,
            "rollback_available": True,
            "next_action_code": failure.next_action_code,
            "next_action_text": failure.next_action_text,
            "unresolved_risk": failure.unresolved_risk,
            "timestamp": now(),
        },
    )


def execute_model_connector(
    workspace_dir: str | Path,
    brain_name: str,
    endpoint_id: str,
    prompt: str,
    *,
    remote_consent: bool = False,
    evidence_slices: Sequence[Mapping[str, Any]] | None = None,
    identity_id: str = DEFAULT_IDENTITY_ID,
    task_id: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir).resolve()
    ensure_local_workspace(workspace)
    brain_root = brain_output_dir(workspace, brain_name).resolve()
    ledger = _ledger_path(workspace, brain_name)
    initialize_operational_ledger(ledger)
    execution_id = f"execution_{uuid.uuid4().hex}"
    resolved_task_id = str(task_id or execution_id)
    execution_run_id: int | None = None
    model_id = ""
    model_family = "CUSTOM_API"
    entry_slip: dict[str, Any] | None = None
    exit_slip_recorded = False
    clean_endpoint_id = str(endpoint_id or "").strip() or "UNRESOLVED"
    call_started_at = now()

    try:
        clean_prompt = _bounded_prompt(prompt)
        bounded_evidence = _bounded_evidence(evidence_slices)
        try:
            profile = get_model_connector(workspace, clean_endpoint_id, identity_id)
        except ModelConnectorError as exc:
            raise _ExecutionFailure(
                "MODEL_CONNECTOR_NOT_AVAILABLE",
                "ENDPOINT_UNREACHABLE",
                False,
                "SELECT_ACTIVE_ENDPOINT",
                "Select or repair an enabled custom endpoint in Settings.",
                "No endpoint request was sent.",
            ) from exc
        model_id = str(profile.get("model_id") or "")
        model_family = _model_family(profile)
        if not profile.get("enabled") or not profile.get("active_prompt_bar") or profile.get("endpoint_type") not in CUSTOM_PROMPT_BAR_TYPES:
            raise _ExecutionFailure(
                "MODEL_CONNECTOR_NOT_ACTIVE",
                "PAYLOAD_REJECTED",
                True,
                "SELECT_ACTIVE_ENDPOINT",
                "Activate one enabled custom endpoint in Settings.",
                "No endpoint request was sent.",
            )
        try:
            versions = list_brain_versions(workspace, brain_name, verify_hashes=True)
        except Exception as exc:
            raise _ExecutionFailure(
                "VERIFIED_BRAIN_VERSION_UNAVAILABLE",
                "HASH_MISMATCH",
                False,
                "REBUILD_AND_VERIFY_BRAIN",
                "Build and verify the brain before direct model execution.",
                "The active brain snapshot cannot be trusted.",
            ) from exc
        latest = (versions.get("versions") or [None])[0]
        if not latest:
            raise _ExecutionFailure(
                "VERIFIED_BRAIN_VERSION_REQUIRED",
                "MODEL_LOAD_FAILED",
                True,
                "RUN_BUILD_COMMAND",
                "Run Build Command to create a verified brain version first.",
                "No endpoint request was sent.",
            )
        snapshot_hash = str(latest["snapshot_hash"])
        try:
            entry_slip = record_model_state_slip(
                brain_root,
                slip_kind="ENTRY",
                model_family=model_family,
                model_name=model_id,
                prompt_index=1,
                gates=["ENV15", "MODEL_ENDPOINT", "CANDIDATE_ONLY"],
                lanes=["research", "chat_lineage"],
                turn_id=f"{execution_id}:ENTRY",
                token_count=None,
            )
        except Exception as exc:
            raise _ExecutionFailure(
                "MODEL_ENTRY_STATE_SLIP_WRITEBACK_FAILED",
                "STATE_TRAVEL_WRITEBACK_FAILED",
                False,
                "REPAIR_SELECTED_BRAIN_STATE_TRAVEL",
                "Repair the selected brain Plan/steer state sector before retrying.",
                "No endpoint request was sent because the required entry slip was not durable.",
                {"exception_type": type(exc).__name__},
            ) from exc
        if profile.get("local_or_remote") == "REMOTE" and not remote_consent:
            raise _ExecutionFailure(
                "REMOTE_ENDPOINT_EXPLICIT_CONSENT_REQUIRED",
                "PAYLOAD_REJECTED",
                True,
                "CONFIRM_REMOTE_ENDPOINT",
                "Confirm the remote endpoint for this explicit bounded request.",
                "No endpoint request was sent.",
            )
        url = _safe_request_url(profile)
        authentication = _authentication_headers(workspace, profile, identity_id)
        payload = _request_payload(profile, clean_prompt, bounded_evidence, snapshot_hash)
        execution_run_id = append_model_execution_run(
            ledger,
            {
                "execution_id": execution_id,
                "brain_id": brain_name,
                "brain_snapshot_hash": snapshot_hash,
                "connector_id": clean_endpoint_id,
                "endpoint_class": str(profile["endpoint_type"]),
                "model_id": model_id,
                "user_request": clean_prompt,
                "evidence_slice_ids": [item["id"] for item in bounded_evidence],
                "sql_queries": [],
                "fts_queries": [],
                "request_timestamp": now(),
                "human_decision": "PENDING",
                "status": "REQUESTED",
            },
        )
        append_endpoint_execution_event(
            ledger,
            {
                "execution_run_id": execution_run_id,
                "event_type": "REQUEST_READY",
                "status": "PASS",
                "usage_classification": "UNAVAILABLE",
            },
        )
        try:
            response = (transport or _post_json)(url, payload, authentication, float(profile["timeout"]))
        except _ExecutionFailure:
            raise
        except Exception as exc:
            raise _classify_transport_error(exc) from exc
        if not isinstance(response, Mapping):
            raise _ExecutionFailure(
                "MODEL_ENDPOINT_RESPONSE_OBJECT_REQUIRED",
                "MODEL_RESPONSE_INVALID",
                True,
                "RETRY_OR_EDIT_RESPONSE_MAPPING",
                "Return a JSON object or correct the endpoint adapter.",
                "The response could not be classified.",
            )
        response_text, provider_response_id, usage, usage_classification, resulting_output_payload = _response_fields(profile, response)
        resulting_output: dict[str, Any] | None = None
        if resulting_output_payload is not None:
            try:
                resulting_output = materialize_model_output(
                    workspace,
                    brain_name,
                    execution_id=execution_id,
                    endpoint_id=clean_endpoint_id,
                    provider_response_id=provider_response_id,
                    baseline_snapshot_hash=snapshot_hash,
                    output_payload=resulting_output_payload,
                )
            except SelectedBrainStateError as exc:
                raise _ExecutionFailure(
                    str(exc),
                    "MODEL_RESPONSE_INVALID",
                    True,
                    "RETRY_WITH_HASHED_OUTPUT",
                    "Return an immutable UTF-8 output with an exact matching SHA-256.",
                    "The response remains candidate-only and no output was indexed.",
                ) from exc
        endpoint_event_id = append_endpoint_execution_event(
            ledger,
            {
                "execution_run_id": execution_run_id,
                "event_type": "RESPONSE_RECEIVED",
                "status": "CANDIDATE",
                "provider_response_id": provider_response_id,
                "usage_classification": usage_classification,
                "usage": usage,
                "response_text": response_text,
            },
        )
        task_run_id = _append_governed_prompt_receipts(
            ledger,
            task_id=resolved_task_id,
            execution_id=execution_id,
            snapshot_hash=snapshot_hash,
            prompt=clean_prompt,
            response_text=response_text,
            endpoint_id=clean_endpoint_id,
            model_id=model_id,
            provider_response_id=provider_response_id,
            usage=usage,
            usage_classification=usage_classification,
            start_timestamp=call_started_at,
            output_sha256=(resulting_output or {}).get("output_sha256"),
        )
        try:
            state_travel = _append_model_state_travel(
                brain_root,
                execution_id=execution_id,
                endpoint_id=clean_endpoint_id,
                model_family=model_family,
                model_name=model_id,
                snapshot_hash=snapshot_hash,
                prompt=clean_prompt,
                response_text=response_text,
                provider_response_id=provider_response_id,
                usage=usage,
                output_record=resulting_output,
            )
            state_travel["entry_exit"]["entry"] = entry_slip
            exit_slip_recorded = True
        except Exception as exc:
            raise _ExecutionFailure(
                "MODEL_STATE_TRAVEL_WRITEBACK_FAILED",
                "STATE_TRAVEL_WRITEBACK_FAILED",
                False,
                "REPAIR_SELECTED_BRAIN_STATE_TRAVEL",
                "Repair Research, Chat Lineage, or Plan state sectors and retry from the preserved response receipt.",
                "The response remains candidate-only because its required brain-sector writeback did not fully validate.",
                {"exception_type": type(exc).__name__},
            ) from exc
        result = {
            "status": "CANDIDATE_RESPONSE",
            "execution_id": execution_id,
            "execution_run_id": execution_run_id,
            "endpoint_event_id": endpoint_event_id,
            "task_run_id": task_run_id,
            "endpoint_id": clean_endpoint_id,
            "endpoint_class": profile["endpoint_type"],
            "model_id": model_id,
            "local_or_remote": profile["local_or_remote"],
            "privacy_classification": profile["privacy_classification"],
            "brain_snapshot_hash": snapshot_hash,
            "response_text": response_text,
            "provider_response_id": provider_response_id,
            "usage": usage,
            "usage_classification": usage_classification,
            "operational_ledger": str(ledger),
            "human_decision": "PENDING",
            "candidate_fused": False,
            "project_data_sent_automatically": False,
            "evidence_slice_count": len(bounded_evidence),
            "state_travel_writeback": state_travel,
        }
        if resulting_output is not None:
            result.update(
                {
                    "output_id": resulting_output["output_id"],
                    "output_sha256": resulting_output["output_sha256"],
                    "output_path": resulting_output["output_path"],
                    "output_receipt_path": resulting_output["receipt_path"],
                    "output_application_status": "PROPOSED_NOT_APPLIED",
                }
            )
        return result
    except _ExecutionFailure as failure:
        if entry_slip is not None and not exit_slip_recorded:
            try:
                record_model_state_slip(
                    brain_root,
                    slip_kind="EXIT",
                    model_family=model_family,
                    model_name=model_id or "unresolved",
                    prompt_index=1,
                    gates=["ENV15", "MODEL_ENDPOINT", f"FAILED_{failure.code}"],
                    lanes=["research", "chat_lineage"],
                    turn_id=f"{execution_id}:EXIT:{failure.code}",
                    token_count=None,
                )
            except Exception:
                pass
        _append_failure(
            ledger,
            failure,
            execution_id=execution_id,
            execution_run_id=execution_run_id,
            endpoint_id=clean_endpoint_id,
            model_id=model_id,
            task_id=resolved_task_id,
        )
        raise ModelExecutionError(failure.code) from failure
    except Exception as exc:
        failure = _classify_transport_error(exc)
        if entry_slip is not None and not exit_slip_recorded:
            try:
                record_model_state_slip(
                    brain_root,
                    slip_kind="EXIT",
                    model_family=model_family,
                    model_name=model_id or "unresolved",
                    prompt_index=1,
                    gates=["ENV15", "MODEL_ENDPOINT", f"FAILED_{failure.code}"],
                    lanes=["research", "chat_lineage"],
                    turn_id=f"{execution_id}:EXIT:{failure.code}",
                    token_count=None,
                )
            except Exception:
                pass
        _append_failure(
            ledger,
            failure,
            execution_id=execution_id,
            execution_run_id=execution_run_id,
            endpoint_id=clean_endpoint_id,
            model_id=model_id,
            task_id=resolved_task_id,
        )
        raise ModelExecutionError(failure.code) from exc


__all__ = ["ModelExecutionError", "execute_model_connector"]
