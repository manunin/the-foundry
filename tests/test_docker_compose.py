from __future__ import annotations

from pathlib import Path


def test_backend_services_use_named_worktree_volume_by_default() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    mount = "${WORKTREE_VOLUME:-foundry-worktrees}:/app/worktrees"
    assert compose.count(mount) == 3
    assert "\nvolumes:\n  foundry-worktrees:\n" in compose
