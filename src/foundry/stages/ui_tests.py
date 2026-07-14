from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from foundry.agents import AgentSettings, AgentStage, AgentTask, make_agent
from foundry.config import Settings
from foundry.models import Task

OUTPUT_DIR = Path(".foundry/ui-tests")
DEPLOY_SKILL = Path(".codex/skills/deploy-mac-mini-json-ui/SKILL.md")
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def run(
    task: Task,
    worktree_path: Path,
    settings: Settings,
    *,
    plan_text: str,
    attempt: int,
) -> dict[str, Any]:
    output_root = worktree_path / OUTPUT_DIR
    try:
        if not (worktree_path / DEPLOY_SKILL).is_file():
            return _infra_result(
                f"required deploy skill is missing: {DEPLOY_SKILL.as_posix()}"
            )

        agent = make_agent(
            AgentSettings.from_env(AgentStage.UI_TESTS, db_path=settings.db_path)
        )
        agent_task = AgentTask(
            id=task.id or task.issue_number,
            title=task.issue_title,
            description=task.issue_body,
        )
        try:
            agent_result = agent.apply(
                task=agent_task,
                worktree=worktree_path,
                input=plan_text,
            )
        except Exception as exc:
            return _infra_result(f"UI-tests agent failed: {exc}")

        try:
            normalized = _load_result(
                output_root,
                settings,
                task_id=task.id or task.issue_number,
                attempt=attempt,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _infra_result(f"invalid UI-tests result: {exc}")
        normalized.update(
            {
                "agent": agent.name,
                "stage": agent_result.stage.value,
                "cost_usd": agent_result.cost_usd,
                "tokens_in": agent_result.tokens_in,
                "tokens_out": agent_result.tokens_out,
            }
        )
        return normalized
    finally:
        shutil.rmtree(output_root, ignore_errors=True)


def _load_result(
    output_root: Path,
    settings: Settings,
    *,
    task_id: int,
    attempt: int,
) -> dict[str, Any]:
    manifest_path = output_root / "result.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("result.json is missing or is not a regular file")
    if manifest_path.stat().st_size > settings.ui_test_artifact_max_file_bytes:
        raise ValueError("result.json exceeds the per-file limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("result.json must use schema version 1")
    status = manifest.get("status")
    if status not in {"passed", "failed"}:
        raise ValueError("status must be passed or failed")
    deployed_url = manifest.get("deployed_url")
    if not isinstance(deployed_url, str) or not deployed_url.strip():
        raise ValueError("deployed_url must be a non-empty string")

    scenarios_value = manifest.get("scenarios")
    if not isinstance(scenarios_value, list) or not scenarios_value:
        raise ValueError("scenarios must be a non-empty array")

    referenced_files: list[Path] = []
    scenarios: list[dict[str, Any]] = []
    screenshot_sources: list[tuple[Path, str, str]] = []
    for index, value in enumerate(scenarios_value):
        if not isinstance(value, dict):
            raise ValueError(f"scenario {index} must be an object")
        name = value.get("name")
        scenario_status = value.get("status")
        duration_ms = value.get("duration_ms")
        error = value.get("error")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"scenario {index} has no name")
        if scenario_status not in {"passed", "failed"}:
            raise ValueError(f"scenario {index} has invalid status")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError(f"scenario {index} has invalid duration_ms")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"scenario {index} has invalid error")
        screenshot_paths = value.get("screenshots", [])
        if not isinstance(screenshot_paths, list) or not all(
            isinstance(item, str) for item in screenshot_paths
        ):
            raise ValueError(f"scenario {index} has invalid screenshots")
        normalized_screenshots: list[str] = []
        for item in screenshot_paths:
            source, relative = _safe_source(output_root, item)
            mime_type = IMAGE_SUFFIXES.get(source.suffix.lower())
            if mime_type is None or not _has_image_signature(source, mime_type):
                raise ValueError(f"screenshot is not a supported image: {item}")
            referenced_files.append(source)
            screenshot_sources.append((source, relative, mime_type))
            normalized_screenshots.append(relative)
        scenarios.append(
            {
                "name": name.strip(),
                "status": scenario_status,
                "duration_ms": duration_ms,
                "error": error or None,
                "screenshots": normalized_screenshots,
            }
        )

    logs_value = manifest.get("logs", {})
    if not isinstance(logs_value, dict):
        raise ValueError("logs must be an object")
    log_sources: dict[str, Path | None] = {}
    for key in ("core", "ui", "browser"):
        path_value = logs_value.get(key)
        if path_value is None:
            log_sources[key] = None
            continue
        if not isinstance(path_value, str):
            raise ValueError(f"logs.{key} must be a relative path")
        source, _ = _safe_source(output_root, path_value)
        referenced_files.append(source)
        log_sources[key] = source

    _validate_limits([manifest_path, *referenced_files], settings)
    log_text = {
        key: _read_tail(source, settings.ui_test_log_max_chars) if source else ""
        for key, source in log_sources.items()
    }
    screenshots = _copy_screenshots(
        screenshot_sources,
        settings,
        task_id=task_id,
        attempt=attempt,
    )
    passed = status == "passed" and all(
        scenario["status"] == "passed" for scenario in scenarios
    )
    failed_names = [
        scenario["name"]
        for scenario in scenarios
        if scenario["status"] == "failed"
    ]
    report = (
        "UI crawler passed"
        if passed
        else "UI crawler failed: " + ", ".join(failed_names)
    )
    return {
        "passed": passed,
        "retryable": not passed,
        "requires_human": False,
        "failure_kind": None if passed else "ui_crawler",
        "report": report,
        "deployed_url": deployed_url.strip(),
        "scenarios": scenarios,
        "screenshots": screenshots,
        "core_logs": log_text["core"],
        "ui_logs": log_text["ui"],
        "browser_logs": log_text["browser"],
    }


def _safe_source(root: Path, raw: str) -> tuple[Path, str]:
    relative = Path(raw)
    if relative.is_absolute() or not raw.strip() or ".." in relative.parts:
        raise ValueError(f"unsafe artifact path: {raw}")
    source = root / relative
    root_resolved = root.resolve()
    source_resolved = source.resolve(strict=True)
    if not source_resolved.is_relative_to(root_resolved):
        raise ValueError(f"artifact escapes output directory: {raw}")
    current = source
    while current != root:
        if current.is_symlink():
            raise ValueError(f"artifact path contains a symlink: {raw}")
        current = current.parent
    if not source_resolved.is_file():
        raise ValueError(f"artifact is not a regular file: {raw}")
    return source_resolved, relative.as_posix()


def _validate_limits(files: list[Path], settings: Settings) -> None:
    unique = list(dict.fromkeys(files))
    if len(unique) > settings.ui_test_artifact_max_files:
        raise ValueError("artifact file count exceeds configured limit")
    total = 0
    for path in unique:
        size = path.stat().st_size
        if size > settings.ui_test_artifact_max_file_bytes:
            raise ValueError(f"artifact exceeds per-file limit: {path.name}")
        total += size
    if total > settings.ui_test_artifact_max_attempt_bytes:
        raise ValueError("artifact bytes exceed per-attempt limit")


def _copy_screenshots(
    sources: list[tuple[Path, str, str]],
    settings: Settings,
    *,
    task_id: int,
    attempt: int,
) -> list[dict[str, Any]]:
    task_root = settings.ui_test_artifact_root / f"task-{task_id}"
    destination = task_root / f"attempt-{attempt}"
    temporary = task_root / f".attempt-{attempt}.tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True, exist_ok=True)
    metadata: list[dict[str, Any]] = []
    try:
        for source, relative, mime_type in sources:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            artifact_path = f"attempt-{attempt}/{relative}"
            metadata.append(
                {
                    "name": source.name,
                    "artifact_path": artifact_path,
                    "mime_type": mime_type,
                    "size_bytes": source.stat().st_size,
                }
            )
        task_root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(destination, ignore_errors=True)
        os.replace(temporary, destination)
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return metadata


def _has_image_signature(path: Path, mime_type: str) -> bool:
    with path.open("rb") as stream:
        header = stream.read(12)
    if mime_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    return header.startswith(b"RIFF") and header[8:12] == b"WEBP"


def _read_tail(path: Path, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - (max_chars * 4)))
        text = stream.read().decode("utf-8", errors="replace")
    return text[-max(0, max_chars) :]


def _infra_result(report: str) -> dict[str, Any]:
    return {
        "passed": False,
        "retryable": False,
        "requires_human": True,
        "failure_kind": "infra",
        "report": report,
        "scenarios": [],
        "screenshots": [],
        "deployed_url": None,
        "core_logs": "",
        "ui_logs": "",
        "browser_logs": "",
    }
