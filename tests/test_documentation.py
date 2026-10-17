from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import AliasChoices

from arrest_report_agent.config import AppSettings
from arrest_report_agent.main import _build_parser

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_documentation_local_links_resolve_inside_the_repository(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    # Ignore code fences, whose examples may intentionally contain Markdown syntax.
    prose = re.sub(r"^```[^\n]*\n.*?^```\s*$", "", text, flags=re.MULTILINE | re.DOTALL)
    for match in re.finditer(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", prose):
        target = match.group(1).strip().removeprefix("<").removesuffix(">")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        resolved = (document.parent / unquote(parsed.path)).resolve()
        assert resolved.is_relative_to(ROOT), f"{document.name}: link leaves repository"
        assert resolved.exists(), f"{document.name}: broken local link {target}"


def test_environment_reference_covers_every_setting_and_supported_alias() -> None:
    reference = (ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", reference))
    expected: set[str] = set()
    for field in AppSettings.model_fields.values():
        if field.alias:
            expected.add(field.alias)
        if isinstance(field.validation_alias, AliasChoices):
            expected.update(alias for alias in field.validation_alias.choices if isinstance(alias, str))
    assert not expected - documented, f"Undocumented environment variables: {sorted(expected - documented)}"


def test_environment_reference_covers_the_template() -> None:
    template = (ROOT / "default.env").read_text(encoding="utf-8")
    variables = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", template, flags=re.MULTILINE))
    reference = (ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", reference))
    assert variables <= documented


def test_environment_template_does_not_ship_populated_secrets_or_personal_quota_access() -> None:
    template = (ROOT / "default.env").read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", template, flags=re.MULTILINE))
    sensitive = {
        key
        for key in values
        if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|ADMIN_KEY|ACCESS_KEY|DATABASE_URL", key)
    }
    for key in sensitive:
        assert not values[key].strip(), f"Template contains a populated secret: {key}"
    assert not values["REPORT_OWNER_QUOTA_ADMIN_USER_IDS"].strip()
    assert values["AUTHORIZED_USER_IDS"] == "111111111111111111"


def test_readme_links_the_installation_configuration_and_operations_guides() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for guide in ("SETUP", "ENVIRONMENT", "CONFIGURATION", "OPERATIONS", "FEATURES"):
        assert f"(docs/{guide}.md)" in readme


def test_discord_setup_documents_both_privileged_intents_and_attachments() -> None:
    for document in (ROOT / "docs" / "SETUP.md", ROOT / "docs" / "PERMISSIONS.md"):
        text = document.read_text(encoding="utf-8")
        assert "Message Content Intent" in text
        assert "Server Members Intent" in text
        assert "Attach Files" in text


def test_installation_database_commands_match_the_cli_without_replacement() -> None:
    setup = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
    commands = re.findall(r"^python -m arrest_report_agent\.main (db [^\n]+)$", setup, re.MULTILINE)
    assert commands == ["db migrate", "db import-env --dry-run", "db import-env --apply", "db verify"]
    parser = _build_parser()
    for command in commands:
        args = parser.parse_args(command.split())
        assert args.command == "db"
        assert not getattr(args, "replace", False)
