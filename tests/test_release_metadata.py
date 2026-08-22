from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_release_metadata_is_synchronized_for_v1_1_0() -> None:
    project_text = (ROOT / "pyproject.toml").read_text()
    project_version_match = re.search(r'^version = "([^"]+)"$', project_text, re.MULTILINE)
    init_text = (ROOT / "src/version_drift/__init__.py").read_text()
    runtime_version = re.search(r'__version__ = "([^"]+)"', init_text)
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert project_version_match is not None
    project_version = project_version_match.group(1)
    assert project_version == "1.1.0"
    assert runtime_version is not None
    assert runtime_version.group(1) == project_version
    assert "## [1.1.0] - 2026-08-23" in changelog
    assert "read-only agent integration board" in changelog.lower()
