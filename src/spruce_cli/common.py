"""Config, sessions and CSV helpers shared by every command."""

import csv
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

HOME_DIR = Path.home() / ".spruce"


class UserError(Exception):
    """Error shown to the user without a traceback."""


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slug(value: str) -> str:
    """https://contoso.sharepoint.com/sites/x -> contoso-sharepoint-com-sites-x"""
    return re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"^https?://", "", value)).strip("-")


def mb_to_gb(mb: float) -> str:
    return f"{mb / 1024:.2f}"


def bytes_to_mb(size: float) -> str:
    return f"{size / 1024**2:.2f}"


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    path: Path | None
    profile: str
    admin_url: str
    tenant: str
    client_id: str
    auth: str
    sessions_dir: Path

    @property
    def token_cache(self) -> Path:
        """One login cache per tenant + app, so profiles of different tenants never mix accounts."""
        return HOME_DIR / f"token-cache_{slug(self.tenant)}_{self.client_id[:8]}.json"


AUTH_MODES = ("browser", "device-code")
GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
ADMIN_URL = re.compile(r"^https://([a-z0-9-]+)-admin\.sharepoint\.com$", re.IGNORECASE)


def config_file(profile: str | None, folder: Path | None = None) -> Path:
    """spruce.toml for the default profile, spruce.<profile>.toml for a named one."""
    name = f"spruce.{profile}.toml" if profile else "spruce.toml"
    return (folder or Path.cwd()) / name


def list_profiles(folder: Path | None = None) -> list[tuple[str, Path]]:
    folder = folder or Path.cwd()
    files = sorted(folder.glob("spruce.toml")) + sorted(folder.glob("spruce.*.toml"))
    return [("default" if f.name == "spruce.toml" else f.name[len("spruce.") : -len(".toml")], f) for f in files]


def load_config(path: Path | None, profile: str | None = None) -> Config:
    """Load --config, else the profile's file (--profile / $SPRUCE_PROFILE), else $SPRUCE_CONFIG or ./spruce.toml."""
    profile = profile or os.environ.get("SPRUCE_PROFILE") or None
    if path is None and profile:
        path = config_file(profile)
        if not path.exists():
            raise UserError(f"Profile '{profile}' not found ({path.name}). Create it with: spruce init {profile}")
    if path is None and os.environ.get("SPRUCE_CONFIG"):
        path = Path(os.environ["SPRUCE_CONFIG"])
    if path is None and config_file(None).exists():
        path = config_file(None)
    data: dict[str, Any] = {}
    if path is not None:
        if not path.exists():
            raise UserError(f"Config file not found: {path}")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.resolve().parent if path else Path.cwd()
    sessions_dir = Path(str(data.get("sessions_dir", "sessions")))
    auth = str(data.get("auth", "browser"))
    if auth not in AUTH_MODES:
        raise UserError("auth must be 'browser' or 'device-code'")
    return Config(
        path=path.resolve() if path else None,
        profile=profile or "default",
        admin_url=str(data.get("admin_url", "")).rstrip("/"),
        tenant=str(data.get("tenant", "organizations")),
        client_id=str(data.get("client_id", "")),
        auth=auth,
        sessions_dir=sessions_dir if sessions_dir.is_absolute() else base / sessions_dir,
    )


def default_tenant(admin_url: str) -> str:
    """https://contoso-admin.sharepoint.com -> contoso.onmicrosoft.com"""
    match = ADMIN_URL.match(admin_url.rstrip("/"))
    return f"{match.group(1)}.onmicrosoft.com" if match else ""


def write_config(
    path: Path, *, admin_url: str, tenant: str, client_id: str, auth: str, sessions_dir: str, force: bool
) -> None:
    """Validate the values and write a commented spruce.toml."""
    admin_url = admin_url.strip().rstrip("/")
    if not ADMIN_URL.match(admin_url):
        raise UserError(f"admin_url must look like https://contoso-admin.sharepoint.com (got {admin_url})")
    if not GUID.match(client_id.strip()):
        raise UserError(f"client_id must be a GUID (got {client_id})")
    if auth not in AUTH_MODES:
        raise UserError("auth must be 'browser' or 'device-code'")
    if path.exists() and not force:
        raise UserError(f"{path} already exists. Use --force to overwrite it.")
    q = json.dumps  # a JSON string is a valid TOML basic string
    path.write_text(
        "# spruce configuration. Created by: spruce init\n\n"
        "# SharePoint admin center URL\n"
        f"admin_url = {q(admin_url)}\n\n"
        "# Tenant ID or domain\n"
        f"tenant = {q(tenant.strip())}\n\n"
        "# Client ID of your Entra ID app (public client, delegated SharePoint AllSites.FullControl,\n"
        "# redirect URI http://localhost)\n"
        f"client_id = {q(client_id.strip())}\n\n"
        '# "browser" opens the browser; "device-code" prints a code for microsoft.com/devicelogin\n'
        f"auth = {q(auth)}\n\n"
        "# Folder for session folders (relative to this file or absolute)\n"
        f"sessions_dir = {q(sessions_dir)}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- sessions


def create_session(config: Config, name: str, note: str = "") -> Path:
    folder = config.sessions_dir / f"{timestamp()}_{slug(name) or 'session'}"
    folder.mkdir(parents=True, exist_ok=False)
    meta = {"name": name, "created_at": iso_now(), "note": note}
    (folder / "session.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return folder


def list_sessions(config: Config) -> list[Path]:
    if not config.sessions_dir.exists():
        return []
    return sorted(p for p in config.sessions_dir.iterdir() if (p / "session.json").exists())


def resolve_session(config: Config, name: str | None) -> Path:
    """Find a session by folder name, name suffix or prefix; default: $SPRUCE_SESSION, then the latest."""
    sessions = list_sessions(config)
    if not sessions:
        raise UserError("No sessions yet. Create one with: spruce session new <name>")
    name = name or os.environ.get("SPRUCE_SESSION")
    if not name:
        latest = sessions[-1]
        print(
            f"Using latest session: {latest.name} (pick another with --session or SPRUCE_SESSION)",
            file=sys.stderr,
        )
        return latest
    matches = [p for p in sessions if p.name == name] or [
        p for p in sessions if p.name.lower().endswith(f"_{slug(name).lower()}") or p.name.startswith(name)
    ]
    if not matches:
        raise UserError(f"Session not found: {name}. List them with: spruce session list")
    if len(matches) > 1:
        raise UserError(f"Ambiguous session '{name}': {', '.join(p.name for p in matches)}")
    return matches[0]


def log_command(session: Path, argv: list[str]) -> None:
    with (session / "history.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{iso_now()}  spruce {' '.join(argv)}\n")


def latest_file(session: Path, prefix: str) -> Path:
    """Newest <prefix>_<timestamp>.csv in the session, e.g. the last site-storage export."""
    files = sorted(session.glob(f"{prefix}_*.csv"))
    if not files:
        raise UserError(f"No {prefix}_*.csv in session {session.name}. Pass the file explicitly.")
    return files[-1]


# ---------------------------------------------------------------- CSV


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        print(f"WARNING: no rows to write to {path}", file=sys.stderr)
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path}")


def append_csv_row(path: Path, row: Mapping[str, Any]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if new:
            writer.writeheader()
        writer.writerow(row)


def confirm(prompt: str, word: str) -> None:
    if not sys.stdin.isatty():
        raise UserError("Confirmation required but no terminal is attached. Use --yes only if you are sure.")
    if input(f"{prompt} (type '{word}' to continue): ").strip() != word:
        raise UserError("Cancelled.")
