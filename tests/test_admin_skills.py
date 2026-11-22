"""Tests for the admin panel's Agent Skills endpoints and tab."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from importlib.resources import files
from pathlib import Path

import pytest

from toolregistry import AdminServer, ToolRegistry

_SKILL_MD = """\
---
name: {name}
description: {description}
---
# {name}

{body}
"""


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "A test skill",
    body: str = "Follow these instructions.",
    resources: dict[str, str] | None = None,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        _SKILL_MD.format(name=name, description=description, body=body),
        encoding="utf-8",
    )
    for relative, content in (resources or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


@pytest.fixture
def skills_server(tmp_path: Path):
    """Run an admin server backed by a registry with two skills."""
    _write_skill(tmp_path, "alpha", body="ALPHA_BODY")
    _write_skill(
        tmp_path,
        "beta",
        description="Beta skill",
        resources={"references/guide.md": "GUIDE_BODY"},
    )
    registry = ToolRegistry()
    registry.discover_skills(tmp_path)
    registry.register_skill_tools()

    server = AdminServer(registry, port=18097)
    info = server.start()
    time.sleep(0.1)
    yield info
    server.stop()


def _request(
    url: str,
    method: str = "GET",
    data: dict | None = None,
) -> tuple[int, dict]:
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_list_skills(skills_server) -> None:
    status, data = _request(f"{skills_server.url}/api/skills")

    assert status == 200
    assert data["count"] == 2
    assert {skill["name"] for skill in data["skills"]} == {"alpha", "beta"}
    assert "skills-list" in data["tools"]
    assert any(root["path"] for root in data["roots"])
    assert data["errors"] == {}
    assert all("instructions" not in skill for skill in data["skills"])


def test_get_skill_detail(skills_server) -> None:
    status, data = _request(f"{skills_server.url}/api/skills/alpha")

    assert status == 200
    assert data["skill"]["name"] == "alpha"
    assert "ALPHA_BODY" in data["skill"]["instructions"]
    assert data["skill"]["scope"] == "custom"
    assert data["skill"]["resources"] == []


def test_get_skill_detail_lists_resources(skills_server) -> None:
    status, data = _request(f"{skills_server.url}/api/skills/beta")

    assert status == 200
    paths = {resource["path"] for resource in data["skill"]["resources"]}
    assert paths == {"references/guide.md"}


def test_read_skill_resource(skills_server) -> None:
    status, data = _request(
        f"{skills_server.url}/api/skills/beta/resources/references/guide.md"
    )

    assert status == 200
    assert data["content"] == "GUIDE_BODY"
    assert data["path"] == "references/guide.md"


def test_read_skill_resource_rejects_traversal(skills_server) -> None:
    status, data = _request(
        f"{skills_server.url}/api/skills/beta/resources/../../etc/passwd"
    )

    assert status == 400
    assert "error" in data


def test_activate_skill(skills_server) -> None:
    status, data = _request(
        f"{skills_server.url}/api/skills/beta/activate",
        method="POST",
        data={"inline_resources": True},
    )

    assert status == 200
    assert data["name"] == "beta"
    assert data["inline_resources"] is True
    assert "<skill_content" in data["prompt"]
    assert "GUIDE_BODY" in data["prompt"]


def test_activate_inline_symlink_escape_is_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRETDATA", encoding="utf-8")
    directory = _write_skill(tmp_path, "alpha")
    (directory / "references").mkdir()
    link = directory / "references" / "escape.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    registry = ToolRegistry()
    registry.discover_skills(tmp_path)
    registry.register_skill_tools()

    server = AdminServer(registry, port=18098)
    info = server.start()
    time.sleep(0.1)
    try:
        status, data = _request(
            f"{info.url}/api/skills/alpha/activate",
            method="POST",
            data={"inline_resources": True},
        )
    finally:
        server.stop()

    assert status == 400
    assert "escapes" in data["message"]


def test_skills_endpoints_return_404_for_unknown_skill(skills_server) -> None:
    for suffix in ("", "/resources/references/guide.md", "/activate"):
        url = f"{skills_server.url}/api/skills/missing{suffix}"
        status, data = _request(url, method="POST" if suffix == "/activate" else "GET")
        assert status == 404
        assert "error" in data


def test_admin_ui_exposes_a_skills_tab() -> None:
    package = files("toolregistry.admin")
    shell = package.joinpath("admin.html").read_text(encoding="utf-8")
    script = package.joinpath("js", "tabs", "skills.js").read_text(encoding="utf-8")

    assert 'data-tab="skills"' in shell
    assert 'id="tab-skills"' in shell
    assert "assets/js/tabs/skills.js" in shell
    assert "function refreshSkills" in script
    assert "activateSkill" in script
