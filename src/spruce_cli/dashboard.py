"""Read-only data for the web UI: everything here works on session files, offline."""

import csv
import io
import json
import shlex
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import msal

from spruce_cli.common import Config, UserError, latest_file, log_command, read_csv
from spruce_cli.quota import build_plan, plan_quotas
from spruce_cli.strategies import STRATEGIES, QuotaOptions
from spruce_cli.targets import SiteFilter, candidate_sites, exclusion_reasons, filter_sites
from spruce_cli.versions import JOBS_FILE


def _float(value: object) -> float:
    try:
        return float(str(value or 0))
    except ValueError:
        return 0.0


def _latest_rows(session: Path, prefix: str) -> tuple[list[dict[str, str]], Path | None]:
    try:
        path = latest_file(session, prefix)
    except UserError:
        return [], None
    return read_csv(path), path


def session_info(session: Path) -> dict[str, Any]:
    meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
    history = session / "history.log"
    return {
        "meta": meta,
        "files": sorted(str(p.relative_to(session)) for p in session.rglob("*") if p.is_file()),
        "history": history.read_text(encoding="utf-8").splitlines() if history.exists() else [],
    }


def sites(
    session: Path, site_filter: SiteFilter, command: str = "cleanup"
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Filtered sites with numeric GB columns, and the ones left out by exclusions (Url, Reason).

    `command` picks what "already done" means for skip_done (see targets.DONE_RULES).
    Both lists are empty if the session has no site data.
    """
    try:
        rows, _ = candidate_sites(session, site_filter)
    except UserError:
        return [], []
    rows = filter_sites(rows, site_filter)
    reasons = exclusion_reasons([r["Url"] for r in rows], session.parent, site_filter, command)
    out = []
    for row in rows:
        if row["Url"] in reasons:
            continue
        out.append(
            {
                "Url": row.get("Url", ""),
                "Title": row.get("Title", ""),
                "ParentTeam": row.get("ParentTeam", ""),
                "Status": row.get("Status", ""),
                "Template": row.get("Template", ""),
                "LastContentModifiedDate": row.get("LastContentModifiedDate", ""),
                "UsedGB": round(_float(row.get("UsedMB")) / 1024, 2),
                "QuotaGB": round(_float(row.get("QuotaMB") or row.get("CurrentQuotaMB")) / 1024, 2),
                "ProposedQuotaGB": round(_float(row.get("ProposedQuotaMB")) / 1024, 2),
                "ExcessGB": round(_float(row.get("ExcessMB")) / 1024, 2),
            }
        )
    return out, [{"Url": url, "Reason": reason} for url, reason in reasons.items()]


def filter_flags(site_filter: SiteFilter) -> str:
    """The CLI options equivalent to a filter, to reuse it in a terminal."""
    parts = []
    if site_filter.status:
        parts.append(f"--status {site_filter.status}")
    if site_filter.min_used_gb is not None:
        parts.append(f"--min-used-gb {site_filter.min_used_gb:g}")
    if site_filter.max_used_gb is not None:
        parts.append(f"--max-used-gb {site_filter.max_used_gb:g}")
    parts += [f"--match '{m}'" for m in site_filter.match]
    parts += [f"--template {t}" for t in site_filter.template]
    if site_filter.inactive_days is not None:
        parts.append(f"--inactive-days {site_filter.inactive_days}")
    if site_filter.top is not None:
        parts.append(f"--top {site_filter.top}")
    parts += [f"--exclude '{e}'" for e in site_filter.exclude]
    if site_filter.skip_done:
        parts.append("--skip-done")
    return " ".join(parts) or "--all"


def report_jobs(sessions_dir: Path) -> list[dict[str, str]]:
    """Version report jobs of every session (Session column first), newest request first."""
    rows = [
        {"Session": path.parent.name, **row} for path in sessions_dir.glob(f"*/{JOBS_FILE}") for row in read_csv(path)
    ]
    return sorted(rows, key=lambda r: r.get("RequestedAt", ""), reverse=True)


def to_csv(rows: list[dict[str, Any]]) -> str:
    """Rows as CSV text, for downloads (same format as the session files)."""
    out = io.StringIO()
    if rows:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return out.getvalue()


def save_plan(session: Path, options: QuotaOptions, strategy: str) -> Path:
    """Write a quota plan to the session, exactly as 'spruce quota plan' does."""
    storage = latest_file(session, "site-storage")
    flags = [f"--{k.replace('_', '-')} {v:g}" for k, v in asdict(options).items()]
    log_command(session, ["quota", "plan", *flags, "--strategy", strategy, " # from spruce ui"])
    return plan_quotas(session, storage, options, strategy, None)


def saved_plans(session: Path) -> list[dict[str, Any]]:
    """Every quota plan summary saved in the session, newest first."""
    rows = []
    for path in sorted(session.glob("quota-plan-summary_*.csv"), reverse=True):
        for row in read_csv(path):
            rows.append({"File": path.name.replace("quota-plan-summary_", "quota-plan_"), **row})
    return rows


def compare_strategies(
    session: Path, options: QuotaOptions
) -> tuple[list[dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """Plan every strategy in memory on the session's latest site export (nothing is written).

    Returns one summary row per strategy and, per strategy, the plan rows by URL.
    """
    storage, path = _latest_rows(session, "site-storage")
    if path is None:
        raise UserError("This session has no site-storage export yet (spruce sites export).")
    summaries, plans = [], {}
    for name in STRATEGIES:
        rows, summary = build_plan(storage, {}, options, name)
        summaries.append(summary)
        plans[name] = {str(r["Url"]): r for r in rows}
    return summaries, plans


def whatif(session: Path) -> tuple[list[dict[str, Any]], Path | None]:
    """Latest what-if summary, with the MB columns converted to GB (FreedMB -> FreedGB, ...)."""
    rows, path = _latest_rows(session, "version-whatif-sites")
    out = []
    for row in rows:
        converted: dict[str, Any] = dict(row)
        for key in ("FreedMB", "SustainableFreedMB", "TemporaryFreedMB", "TotalMB", "HistoricalMB", "PlanExcessMB"):
            converted[key.removesuffix("MB") + "GB"] = round(_float(converted.pop(key, 0)) / 1024, 2)
        out.append(converted)
    return out, path


def cleanup(session: Path) -> tuple[list[dict[str, Any]], Path | None]:
    rows, path = _latest_rows(session, "cleanup-summary")
    out = []
    for row in rows:
        out.append(
            {
                "SiteUrl": row.get("SiteUrl", ""),
                "UsedGB": round(_float(row.get("UsedMB")) / 1024, 2),
                "RecycleStage1GB": round(_float(row.get("RecycleFirstStageMB")) / 1024, 2),
                "RecycleStage2GB": round(_float(row.get("RecycleSecondStageMB")) / 1024, 2),
                "PreservationHoldGB": round(_float(row.get("PreservationHoldMB")) / 1024, 2),
                "HeldPercentOfUsed": row.get("HeldPercentOfUsed", ""),
                "Message": row.get("Message", ""),
            }
        )
    return sorted(out, key=lambda r: -(r["RecycleStage1GB"] + r["RecycleStage2GB"] + r["PreservationHoldGB"])), path


def signed_in_account(config: Config) -> str | None:
    """Account in the login cache, read without network calls (None if not logged in)."""
    if not config.token_cache.exists():
        return None
    cache = msal.SerializableTokenCache()
    cache.deserialize(config.token_cache.read_text(encoding="utf-8"))
    accounts = list(cache.search(msal.TokenCache.CredentialType.ACCOUNT))
    return accounts[0].get("username") if accounts else None


# ------------------------------------------------------------ command builder

# Commands that change the tenant: the builder suggests --dry-run first.
CHANGES_TENANT = {
    "quota apply",
    "versions report-start",
    "versions policy",
    "versions trim",
    "cleanup purge-recycle-bin",
}


@dataclass
class Param:
    name: str
    flag: str  # '--mode'; empty for positional arguments
    kind: str  # flag | choice | multi | value | argument
    help: str = ""
    required: bool = False
    default: Any = None
    choices: list[str] = field(default_factory=list)
    no_flag: str = ""  # '--no-browser' for flags that default to on


def cli_commands() -> dict[str, tuple[str, list[Param]]]:
    """'quota apply' -> (help, params) for every CLI command, read from the Typer app itself."""
    import typer

    from spruce_cli.cli import app

    out: dict[str, tuple[str, list[Param]]] = {}

    def walk(command: Any, path: list[str]) -> None:
        if hasattr(command, "commands"):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])
            return
        if path == ["ui"]:
            return
        params = []
        for p in command.params:
            if p.name in ("help",) or getattr(p, "hidden", False):
                continue
            annotation = getattr(getattr(p, "type_descriptor", None), "annotation", None)
            choices = (
                [m.value for m in annotation] if isinstance(annotation, type) and issubclass(annotation, Enum) else []
            )
            default = p.default.value if isinstance(p.default, Enum) else p.default
            if not p.opts[0].startswith("-"):
                kind = "argument"
            elif getattr(p, "is_flag", False):
                kind = "flag"
            elif choices:
                kind = "choice"
            elif p.multiple:
                kind = "multi"
            else:
                kind = "value"
            params.append(
                Param(
                    name=p.name,
                    flag="" if kind == "argument" else p.opts[0],
                    kind=kind,
                    help=getattr(p, "help", None) or "",
                    required=p.required,
                    default=default,
                    choices=choices,
                    no_flag=p.secondary_opts[0] if p.secondary_opts else "",
                )
            )
        out[" ".join(path)] = ((command.help or "").strip().split("\n")[0], params)

    walk(typer.main.get_command(app), [])
    return out


def build_command(
    path: str, params: list[Param], values: dict[str, Any], session: str | None = None, profile: str | None = None
) -> str:
    """The shell command for a CLI path and form values; options left at their default are omitted."""
    parts = ["spruce"]
    if profile:
        parts += ["-p", profile]
    if session:
        parts += ["-s", session]
    parts += path.split()
    for p in params:
        value = values.get(p.name)
        if p.kind == "flag":
            if value and not p.default:
                parts.append(p.flag)
            elif not value and p.default and p.no_flag:
                parts.append(p.no_flag)
        elif p.kind == "multi":
            lines = value.splitlines() if isinstance(value, str) else list(value or [])
            for item in (line.strip() for line in lines):
                if item:
                    parts += [p.flag, item]
        elif value not in (None, "") and str(value) != str(p.default):
            text = f"{value:g}" if isinstance(value, float) else str(value)
            parts += [text] if p.kind == "argument" else [p.flag, text]
    return shlex.join(parts)
