"""Validate stable tags or stamp PEP 440 alphas from explicit tags or dispatch runs."""

import argparse
import re
import tomllib
from pathlib import Path

_STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_PROJECT = re.compile(r"(?ms)^\[project\][ \t]*\n.*?(?=^\[|\Z)")
_VERSION = re.compile(r"(?m)^([ \t]*version[ \t]*=[ \t]*)([\"'])([^\"'\r\n]+)\2([^\r\n]*)$")


def prepare_release(document: str, *, mode: str, tag: str = "", run_number: str = "") -> tuple[str, str]:
    """Return the release TOML and version; stable releases never rewrite metadata."""
    project = tomllib.loads(document).get("project")
    if not isinstance(project, dict) or project.get("name") != "nagents-channel-telegram-bot":
        raise ValueError("Expected the nagents-channel-telegram-bot project")
    base = project.get("version")
    if not isinstance(base, str) or not _STABLE.fullmatch(base):
        raise ValueError("project.version must be a canonical X.Y.Z release base")
    if mode == "stable":
        if tag != f"v{base}":
            raise ValueError("Stable release tag must equal v + project.version")
        return document, base
    if mode != "alpha":
        raise ValueError("Release mode must be alpha or stable")
    if tag:
        if not re.fullmatch(rf"v{re.escape(base)}a[1-9][0-9]*", tag):
            raise ValueError("Alpha tag must equal v + project.version + a<positive integer>")
        version = tag[1:]
    else:
        if not re.fullmatch(r"[1-9][0-9]*", run_number):
            raise ValueError("Alpha release requires a positive GitHub run_number")
        version = f"{base}a{run_number}"
    section = _PROJECT.search(document)
    if section is None:
        raise ValueError("Expected an explicit [project] section")
    block = section.group()

    def replace(match: re.Match[str]) -> str:
        return f"{match[1]}{match[2]}{version}{match[2]}{match[4]}"

    block, count = _VERSION.subn(replace, block)
    if count != 1:
        raise ValueError("Expected exactly one literal project.version field")
    prepared = document[: section.start()] + block + document[section.end() :]
    if tomllib.loads(prepared)["project"]["version"] != version:
        raise ValueError("Release metadata verification failed")
    return prepared, version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("alpha", "stable"), required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--run-number", default="")
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()
    path = Path(args.project)
    original = path.read_text(encoding="utf-8")
    try:
        prepared, version = prepare_release(original, mode=args.mode, tag=args.tag, run_number=args.run_number)
    except ValueError as error:
        parser.error(str(error))
    if prepared != original:
        path.write_text(prepared, encoding="utf-8")
    print(f"Release version: {version}")


if __name__ == "__main__":
    main()
