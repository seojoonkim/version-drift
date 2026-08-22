"""Release-level contracts that must stay coherent throughout VersionDrift 1.x."""
from __future__ import annotations

import ast
from pathlib import Path
import re


import version_drift


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.1.0"
PUBLIC_SCHEMAS = {
    "version-drift/1",
    "version-drift/scan/1",
    "version-drift/sync/1",
    "version-drift/plan/1",
    "version-drift/doctor/1",
}


def _project() -> dict:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("[project.urls]", 1)[0]
    version = re.search(r'^version = "([^"]+)"$', project, re.M)
    python = re.search(r'^requires-python = "([^"]+)"$', project, re.M)
    return {
        "version": version.group(1) if version else None,
        "requires-python": python.group(1) if python else None,
        "classifiers": project,
    }


def test_v1_version_python_floor_and_maturity_are_exact_and_coherent():
    project = _project()
    assert project["version"] == VERSION
    assert version_drift.__version__ == VERSION
    assert project["requires-python"] == ">=3.9"
    assert "Programming Language :: Python :: 3.9" in project["classifiers"]
    assert '"Development Status :: 5 - Production/Stable"' in project["classifiers"]
    assert '"Development Status :: 3 - Alpha"' not in project["classifiers"]
    assert '"Development Status :: 4 - Beta"' not in project["classifiers"]


def test_release_contract_documents_exist_and_name_frozen_contracts():
    compatibility = (ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")
    threat_model = (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

    for schema in PUBLIC_SCHEMAS:
        assert schema in compatibility
    for code in ("0", "1", "2", "3"):
        assert re.search(rf"(?:exit|code)[^\n]*`{code}`|`{code}`[^\n]*(?:exit|code)", compatibility, re.I)
    for outcome in ("complete", "partial", "failed"):
        assert f"`{outcome}`" in compatibility
    assert "additive" in compatibility.lower()
    assert "authorization" in compatibility.lower()
    assert "malformed" in compatibility.lower()
    assert "read-only" in compatibility.lower()

    assert "trust boundar" in threat_model.lower()
    assert "TOCTOU" in threat_model
    assert "not authorization" in threat_model.lower()
    assert "not auto-stolen" in threat_model.lower()

    for token in ("events.jsonl", "inbox_snapshot.json", "locks", "doctor", "outcome_unknown"):
        assert token in operations
    assert "no apply process is active" in operations.lower()


def test_readme_and_changelog_expose_v1_safety_surface():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    required = {
        "local-only", "fail-closed", "sync --plan", "--fetch", "--no-fetch",
        "git pull --ff-only", "doctor", "version-drift/scan/1",
        "version-drift/sync/1", "version-drift/plan/1", "version-drift/doctor/1",
    }
    assert not (required - set(token for token in required if token in readme))
    for forbidden_operation in ("reset", "stash", "clean", "merge", "rebase", "push", "force"):
        assert forbidden_operation in readme.lower()
    assert "## [1.1.0]" in changelog
    assert "additive" in changelog.lower()


def test_release_workflow_deterministically_checks_tag_and_runtime_version():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "python-version: '3.11'" in workflow
    assert 'assert tag == f"v{project}"' in workflow
    assert "assert runtime == project" in workflow


def test_runtime_version_literal_is_static_for_release_tooling():
    module = ast.parse((ROOT / "src" / "version_drift" / "__init__.py").read_text(encoding="utf-8"))
    assignments = [
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    ]
    assert len(assignments) == 1
    assert ast.literal_eval(assignments[0].value) == VERSION
