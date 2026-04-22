from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Task


class UnsupportedAction(ValueError):
    pass


def run(task: Task, plan: dict, worktree_path: Path, settings: Settings) -> dict:
    """STUB: applies hardcoded plan steps directly on the filesystem.

    Day 3+ will delegate this to Aider / Claude Code SDK. The `plan` shape is
    kept compatible so that drop-in replacement is mechanical.
    """
    applied: list[dict] = []
    for step in plan.get("steps", []):
        action = step.get("action")
        target = worktree_path / step["file"]
        if action == "append_line":
            line = step["line"].rstrip("\n") + "\n"
            with target.open("a", encoding="utf-8") as f:
                f.write(line)
            applied.append({"file": step["file"], "action": action, "bytes": len(line)})
        else:
            raise UnsupportedAction(f"action not supported in skeleton: {action!r}")
    return {"applied": applied}
