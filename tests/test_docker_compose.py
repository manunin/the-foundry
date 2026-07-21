from __future__ import annotations

from pathlib import Path


def test_backend_services_use_named_worktree_volume_by_default() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    mount = "${WORKTREE_VOLUME:-foundry-worktrees}:/app/worktrees"
    assert compose.count(mount) == 3
    assert "\nvolumes:\n  foundry-worktrees:\n" in compose


def test_backend_services_wire_openspec_cli_install_and_telemetry() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    assert compose.count("INSTALL_OPENSPEC_CLI: ${INSTALL_OPENSPEC_CLI:-false}") == 3
    assert compose.count('OPENSPEC_TELEMETRY: "0"') == 3


def test_worker_alone_mounts_operator_ssh_directory_read_only() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    mount = "${HOST_SSH_DIR:-./.docker/ssh}:/root/.ssh:ro"
    assert compose.count(mount) == 1
    worker = compose.split("  worker:", 1)[1].split("  pr-feedback:", 1)[0]
    assert mount in worker
    assert "BEGIN OPENSSH PRIVATE KEY" not in compose


def test_worker_alone_loads_svfm_deployment_environment() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    env_file = "${SVFM_ENV_FILE:-C:/Users/TEMP.BPC/.svfm-mac-mini.env}"
    worker = compose.split("  worker:", 1)[1].split("  pr-feedback:", 1)[0]
    assert f"      - {env_file}\n" in worker


def test_worker_alone_mounts_svfm_environment_file_read_only() -> None:
    compose_path = Path(__file__).parents[1] / "docker-compose.yml"
    compose = compose_path.read_text(encoding="utf-8")

    mount = (
        "${SVFM_ENV_FILE:-C:/Users/TEMP.BPC/.svfm-mac-mini.env}:"
        "/root/.svfm-mac-mini.env:ro"
    )
    assert compose.count(mount) == 1
    worker = compose.split("  worker:", 1)[1].split("  pr-feedback:", 1)[0]
    assert mount in worker
