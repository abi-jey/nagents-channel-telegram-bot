import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from tools.release import prepare_release

_PROJECT = """[project]
name = "nagents-channel-telegram-bot"
version = "0.1.0"  # release base
dependencies = ["nagents>=0.6.0a1,<0.7", "aiohttp>=3.11,<4"]

[tool.example]
version = "unrelated"
"""


@pytest.mark.parametrize("number", ["1", "42", "1000000"])
def test_alpha_is_pep440_and_changes_only_project_version(number: str) -> None:
    document, version = prepare_release(_PROJECT, mode="alpha", run_number=number)
    assert version == f"0.1.0a{number}"
    assert Version(version).pre == ("a", int(number))
    assert Version(version).is_prerelease
    assert document == _PROJECT.replace('version = "0.1.0"', f'version = "{version}"')
    assert tomllib.loads(document)["tool"]["example"]["version"] == "unrelated"
    assert prepare_release(_PROJECT, mode="alpha", run_number=number) == (document, version)


def test_distinct_dispatch_runs_have_distinct_ordered_versions() -> None:
    _, first = prepare_release(_PROJECT, mode="alpha", run_number="10")
    _, second = prepare_release(_PROJECT, mode="alpha", run_number="11")
    assert Version(first) < Version(second) < Version("0.1.0")


@pytest.mark.parametrize("number", ["", "0", "01", "-1", "1.0", " 1", "\uff11", "1\n"])
def test_alpha_requires_valid_run_identity(number: str) -> None:
    with pytest.raises(ValueError, match="run_number"):
        prepare_release(_PROJECT, mode="alpha", run_number=number)


def test_alpha_cannot_take_a_stable_tag() -> None:
    with pytest.raises(ValueError, match="Alpha tag"):
        prepare_release(_PROJECT, mode="alpha", run_number="42", tag="v0.1.0")


@pytest.mark.parametrize("number", ["1", "42", "1000000"])
def test_alpha_tag_stamps_its_version_not_the_workflow_run_number(number: str) -> None:
    tag = f"v0.1.0a{number}"
    document, version = prepare_release(_PROJECT, mode="alpha", tag=tag, run_number="9999999")
    assert version == tag[1:]
    assert Version(version).pre == ("a", int(number))
    assert Version(version) < Version("0.1.0")
    assert document == _PROJECT.replace('version = "0.1.0"', f'version = "{version}"')
    assert prepare_release(_PROJECT, mode="alpha", tag=tag) == (document, version)


@pytest.mark.parametrize(
    "tag",
    [
        "v0.2.0a1",
        "v0.1.1a1",
        "v0.1.0a0",
        "v0.1.0a01",
        "v0.1.0b1",
        "v0.1.0rc1",
        "v0.1.0a1.dev1",
        "v0.1.0a1+local",
        "v0.1.0a1.post1",
        "0.1.0a1",
        "v0.1.0a1\n",
    ],
)
def test_alpha_tag_requires_matching_release_base_and_canonical_alpha(tag: str) -> None:
    with pytest.raises(ValueError, match="Alpha tag"):
        prepare_release(_PROJECT, mode="alpha", tag=tag, run_number="42")


def test_stable_tag_is_exact_and_never_modifies_source() -> None:
    assert prepare_release(_PROJECT, mode="stable", tag="v0.1.0") == (_PROJECT, "0.1.0")


@pytest.mark.parametrize("tag", ["", "0.1.0", "v0.1.1", "v0.1.0a1", "v0.1.0+local", "v0.1.0\n"])
def test_stable_rejects_mismatched_and_prerelease_tags(tag: str) -> None:
    with pytest.raises(ValueError, match="Stable release tag"):
        prepare_release(_PROJECT, mode="stable", tag=tag)


@pytest.mark.parametrize("base", ["0.1.0a1", "0.1.0.dev1", "0.1.0+local", "01.1.0", "0.1", "1!0.1.0"])
@pytest.mark.parametrize("mode", ["alpha", "stable"])
def test_release_base_cannot_bypass_release_policy(base: str, mode: str) -> None:
    with pytest.raises(ValueError, match="release base"):
        prepare_release(_PROJECT.replace('"0.1.0"', f'"{base}"'), mode=mode, run_number="1", tag=f"v{base}")


def test_project_identity_and_mode_are_checked() -> None:
    with pytest.raises(ValueError, match="Expected the"):
        prepare_release(_PROJECT.replace("nagents-channel-telegram-bot", "another-project"), mode="stable")
    with pytest.raises(ValueError, match="mode"):
        prepare_release(_PROJECT, mode="production")


@pytest.mark.parametrize("tag", ["", "v0.1.0a1"])
def test_alpha_cli_stamps_file_and_failure_does_not_modify_it(tmp_path: Path, tag: str) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(_PROJECT, encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "tools" / "release.py"
    command = [sys.executable, str(script), "--project", str(path)]
    subprocess.run([*command, "--mode", "alpha", "--tag", tag, "--run-number", "42"], check=True, capture_output=True)
    alpha = path.read_text(encoding="utf-8")
    assert tomllib.loads(alpha)["project"]["version"] == (tag[1:] if tag else "0.1.0a42")
    failed = subprocess.run([*command, "--mode", "stable", "--tag", "v0.1.0a42"], capture_output=True, check=False)
    assert failed.returncode != 0
    assert path.read_text(encoding="utf-8") == alpha


def test_dependency_requires_the_attachment_core_line_and_caps_the_next_minor() -> None:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]["dependencies"]
    core = next(Requirement(value) for value in dependencies if Requirement(value).name == "nagents")
    assert Version("0.8.0") in core.specifier
    assert Version("0.8.7") in core.specifier
    assert Version("0.7.9") not in core.specifier
    assert Version("0.6.0a24202") not in core.specifier
    assert Version("0.9.0") not in core.specifier
