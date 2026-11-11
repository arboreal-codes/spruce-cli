"""Read-only data for the web UI: everything here works on session files, offline."""

import json
from pathlib import Path
from typing import Any

from spruce_cli.common import UserError, latest_file, read_csv
from spruce_cli.quota import build_plan
from spruce_cli.strategies import STRATEGIES, QuotaOptions
from spruce_cli.targets import SiteFilter, candidate_sites, filter_sites


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


def sites(session: Path, site_filter: SiteFilter) -> list[dict[str, Any]]:
    """Filtered sites with numeric GB columns for tables and charts. Empty if the session has no site data."""
    try:
        rows, _ = candidate_sites(session, site_filter)
    except UserError:
        return []
    out = []
    for row in filter_sites(rows, site_filter):
        out.append(
            {
                "Url": row.get("Url", ""),
                "Title": row.get("Title", ""),
                "Status": row.get("Status", ""),
                "Template": row.get("Template", ""),
                "LastContentModifiedDate": row.get("LastContentModifiedDate", ""),
                "UsedGB": round(_float(row.get("UsedMB")) / 1024, 2),
                "QuotaGB": round(_float(row.get("QuotaMB") or row.get("CurrentQuotaMB")) / 1024, 2),
                "ProposedQuotaGB": round(_float(row.get("ProposedQuotaMB")) / 1024, 2),
                "ExcessGB": round(_float(row.get("ExcessMB")) / 1024, 2),
            }
        )
    return out


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
    return " ".join(parts) or "--all"


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
