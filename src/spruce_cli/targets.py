"""Site selection shared by every command that acts on sites: filters and exclusions.

Candidates come from the session's latest site-storage export (Template, last content
change) overlaid with its latest quota plan (Status, ExcessMB). Explicit --site URLs skip
the filters but not the exclusions.
"""

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from spruce_cli.common import UserError, latest_file, read_csv


@dataclass
class SiteFilter:
    site: list[str] = field(default_factory=list)
    plan: Path | None = None
    storage: Path | None = None
    status: str | None = None
    min_used_gb: float | None = None
    max_used_gb: float | None = None
    match: list[str] = field(default_factory=list)
    template: list[str] = field(default_factory=list)
    inactive_days: int | None = None
    top: int | None = None
    all_sites: bool = False
    exclude: list[str] = field(default_factory=list)
    exclude_file: list[Path] = field(default_factory=list)
    skip_done: bool = False

    def has_criteria(self) -> bool:
        return any(
            [
                self.status,
                self.min_used_gb is not None,
                self.max_used_gb is not None,
                self.match,
                self.template,
                self.inactive_days is not None,
                self.top is not None,
            ]
        )


# What "already done" means per command: files to scan in every session, URL column, success test.
DoneRule = tuple[str, str, Callable[[dict[str, str]], bool]]
DONE_RULES: dict[str, DoneRule] = {
    "cleanup": ("cleanup-summary_*.csv", "SiteUrl", lambda r: not r.get("Message")),
    "report": ("version-report-jobs.csv", "SiteUrl", lambda r: r.get("Status") == "Downloaded"),
    "policy": ("version-policy-log_*.csv", "SiteUrl", lambda r: r.get("Action") == "Applied"),
    "trim": ("version-trim-log.csv", "SiteUrl", lambda r: r.get("Action") == "Queued"),
    "purge": ("recycle-bin-purge-log.csv", "SiteUrl", lambda r: r.get("Action") == "Deleted"),
    "quota": ("quota-apply-log_*.csv", "Url", lambda r: r.get("Action") == "Applied"),
}


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def done_sites(sessions_dir: Path, command: str) -> dict[str, str]:
    """URL -> session name, for sites the command already processed successfully in any session."""
    pattern, column, ok = DONE_RULES[command]
    done: dict[str, str] = {}
    for path in sorted(sessions_dir.glob(f"*/{pattern}")):
        for row in read_csv(path):
            if ok(row) and row.get(column):
                done[_norm(row[column])] = path.parent.name
    return done


def read_exclude_file(path: Path) -> list[str]:
    """URLs from a CSV (Url or SiteUrl column, e.g. any spruce output) or a plain list, one per line."""
    if not path.exists():
        raise UserError(f"Exclude file not found: {path}")
    if path.suffix.lower() == ".csv":
        rows = read_csv(path)
        column = next((c for c in ("Url", "SiteUrl") if rows and c in rows[0]), None)
        if column is None:
            raise UserError(f"{path} has no Url or SiteUrl column")
        return [r[column] for r in rows if r[column]]
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def candidate_sites(session: Path, f: SiteFilter) -> tuple[list[dict[str, Any]], list[Path]]:
    """Latest site-storage joined with the latest quota plan (or the given files). Returns (rows, sources)."""
    storage = f.storage or _latest_or_none(session, "site-storage")
    plan = f.plan or _latest_or_none(session, "quota-plan")
    if storage is None and plan is None:
        raise UserError("No site-storage or quota-plan in the session. Run 'spruce sites export' or pass --site.")
    sources = [p for p in (storage, plan) if p is not None]
    by_url: dict[str, dict[str, Any]] = {}
    for source in sources:
        for row in read_csv(source):
            by_url.setdefault(_norm(row["Url"]), {}).update(row)
    if f.status and plan is None:
        raise UserError("--status needs a quota plan in the session (or --plan).")
    return list(by_url.values()), sources


def filter_sites(rows: list[dict[str, Any]], f: SiteFilter) -> list[dict[str, Any]]:
    """Apply the filters (not the exclusions); largest first, then --top."""
    today = date.today()
    kept = sorted((r for r in rows if _keep(r, f, today)), key=lambda r: -float(r.get("UsedMB") or 0))
    return kept[: f.top] if f.top is not None else kept


def _latest_or_none(session: Path, prefix: str) -> Path | None:
    try:
        return latest_file(session, prefix)
    except UserError:
        return None


def _keep(row: dict[str, Any], f: SiteFilter, today: date) -> bool:
    used_gb = float(row.get("UsedMB") or 0) / 1024
    if f.status and row.get("Status") not in {s.strip() for s in f.status.split(",")}:
        return False
    if f.min_used_gb is not None and used_gb < f.min_used_gb:
        return False
    if f.max_used_gb is not None and used_gb > f.max_used_gb:
        return False
    if f.match:
        text = f"{row.get('Url', '')} {row.get('Title', '')}".lower()
        if not any(fnmatch.fnmatch(text, f"*{p.lower()}*") for p in f.match):
            return False
    if f.template and row.get("Template", "").split("#")[0].upper() not in {
        t.split("#")[0].upper() for t in f.template
    }:
        return False
    if f.inactive_days is not None:
        modified = row.get("LastContentModifiedDate", "")
        try:
            if date.fromisoformat(modified[:10]) > today - timedelta(days=f.inactive_days):
                return False
        except ValueError:
            return False
    return True


def exclusion_reasons(urls: list[str], sessions_dir: Path, f: SiteFilter, command: str) -> dict[str, str]:
    """URL -> why it is left out: 'excluded', or 'done in <session>' (only with skip_done)."""
    patterns = [_norm(p) for p in f.exclude]
    for path in f.exclude_file:
        patterns += [_norm(u) for u in read_exclude_file(path)]
    done = done_sites(sessions_dir, command) if f.skip_done else {}
    reasons: dict[str, str] = {}
    for url in urls:
        key = _norm(url)
        if any(fnmatch.fnmatch(key, p) for p in patterns):
            reasons[url] = "excluded"
        elif key in done:
            reasons[url] = f"done in {done[key]}"
    return reasons


def select_sites(session: Path, sessions_dir: Path, f: SiteFilter, command: str) -> list[str]:
    """Apply filters, then exclusions; print why sites were left out. Largest sites first."""
    info: dict[str, dict[str, Any]] = {}
    if f.site:
        urls = [s.rstrip("/") for s in f.site]
    else:
        if not (f.has_criteria() or f.all_sites):
            raise UserError(
                "Choose the sites: --site URL, or filters such as --status OverCap, --min-used-gb 80, "
                "--top 10, --match '*Proyectos*' (use --all for every site)."
            )
        candidates, sources = candidate_sites(session, f)
        for source in sources:
            print(f"Sites from {source.name}")
        rows = filter_sites(candidates, f)
        urls = [r["Url"].rstrip("/") for r in rows]
        info = {r["Url"].rstrip("/"): r for r in rows}

    skipped = exclusion_reasons(urls, sessions_dir, f, command)
    selected: list[str] = []
    for url in dict.fromkeys(urls):
        if url in skipped:
            print(f"  skipped   {url}  ({skipped[url]})")
        else:
            selected.append(url)
            row = info.get(url, {})
            used = f"{float(row['UsedMB']) / 1024:8.1f} GB" if row.get("UsedMB") else " " * 11
            print(f"  selected  {used}  {row.get('Status', ''):<9} {url}")
    if not selected:
        raise UserError("No sites left after filters and exclusions.")
    print(f"Selected {len(selected)} site(s)")
    return selected
