from pathlib import Path

import pytest
from typer.testing import CliRunner

from spruce_cli.cli import app
from spruce_cli.common import list_profiles, load_config

APP_ID = "11111111-2222-3333-4444-555555555555"


def init(*args: str) -> str:
    result = CliRunner().invoke(app, ["init", *args], input="\n\n")
    return result.output + (str(result.exception) if result.exception else "")


def test_init_creates_independent_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPRUCE_PROFILE", raising=False)
    monkeypatch.delenv("SPRUCE_CONFIG", raising=False)
    init("--admin-url", "https://contoso-admin.sharepoint.com", "--client-id", APP_ID, "--auth", "browser")
    # tenant is prompted with a default derived from the admin URL; accept it with Enter
    result = CliRunner().invoke(
        app, ["init", "fabrikam", "--admin-url", "https://fabrikam-admin.sharepoint.com", "--client-id", APP_ID],
        input="\n\n",
    )  # fmt: skip
    assert result.exit_code == 0, result.output

    default, fabrikam = load_config(None), load_config(None, "fabrikam")
    assert default.tenant == "contoso.onmicrosoft.com"
    assert fabrikam.tenant == "fabrikam.onmicrosoft.com"
    assert fabrikam.auth == "browser"
    assert default.sessions_dir == tmp_path / "sessions"
    assert fabrikam.sessions_dir == tmp_path / "sessions-fabrikam"
    assert default.token_cache != fabrikam.token_cache
    assert [name for name, _ in list_profiles(tmp_path)] == ["default", "fabrikam"]

    assert "already exists" in init("fabrikam", "--admin-url", "https://x-admin.sharepoint.com", "--client-id", APP_ID)
    assert "admin_url must look like" in init(
        "other", "--admin-url", "https://x.sharepoint.com", "--client-id", APP_ID, "--tenant", "x", "--auth", "browser"
    )
    assert not (tmp_path / "spruce.other.toml").exists()


def test_unknown_profile_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="spruce init nope"):
        load_config(None, "nope")
