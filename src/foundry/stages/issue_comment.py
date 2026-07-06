from __future__ import annotations

from pathlib import Path

from langfuse import observe

from foundry import shell  # noqa: F401
from foundry.config import Settings
from foundry.forges import ForgeProvider, provider_for
from foundry.models import Task


@observe(name="stage.issue_comment")
def run(
    task: Task,
    settings: Settings,
    body: str,
    *,
    cwd: Path | None = None,
    provider: ForgeProvider | None = None,
) -> dict:
    """Ask for human input by commenting on the source issue."""
    comment = body.strip()
    if not comment:
        comment = "The agent needs human input before it can continue."

    del cwd
    (provider or provider_for(settings)).comment_issue(
        task.repo, task.issue_number, comment
    )
    return {"issue_number": task.issue_number, "comment": comment}
