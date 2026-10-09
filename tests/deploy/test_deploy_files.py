"""Valida que los artefactos de despliegue existan y sean coherentes con la configuracion."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_env_example_documents_every_setting() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^VAI_([A-Z0-9_]+)=", text, flags=re.M))
    expected = {name.upper() for name in Settings.model_fields}
    assert expected - documented == set()
    assert documented - expected == set()


def test_compose_files_are_valid_yaml_with_services() -> None:
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert {"web", "worker", "db", "redis", "migrate"} <= set(base["services"])
    assert base["services"]["db"]["image"].startswith("postgres:16")
    prod = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "caddy" in prod and "VAI_COOKIE_SECURE" in prod


def test_dockerfile_non_root_with_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER app" in text
    assert "HEALTHCHECK" in text
    assert text.count("FROM ") == 2


def test_ci_workflow_runs_quality_gates() -> None:
    wf = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for tool in ("ruff check", "pytest", "pip-audit", "bandit"):
        assert tool in wf


def test_makefile_targets_and_docs_exist() -> None:
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("dev:", "test:", "lint:", "migrate:", "seed:", "create-owner:"):
        assert target in mk
    assert (ROOT / "docs/runbook.md").exists()
    assert "Twilio" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_no_wildcard_forwarded_ips_in_image() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"*"' not in text
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in text


def test_dev_compose_binds_loopback_only() -> None:
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert all(p.startswith("127.0.0.1:") for p in base["services"]["web"]["ports"])


def test_backup_script_fails_loudly() -> None:
    sh = (ROOT / "deploy/backup.sh").read_text(encoding="utf-8")
    assert "umask 077" in sh and "gzip -t" in sh and "exit 1" in sh
