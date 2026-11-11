"""What-if simulation of version policies over version storage reports (step 5). Offline.

- Versions of a file are ordered newest first by (MajorVersion, MinorVersion).
  The newest one is the current version and is never deleted.
- Count limit: a version is deleted when at least <limit> newer major versions exist
  (the current version counts).
- Age limit: a historical version is deleted when its SnapshotDate is older than
  as_of - <days>. Manual expiration trims also delete every version snapshotted before
  2023-01-01, so that is modeled too.
- Combined: deleted if either rule applies.
- Count limits below 100 are trim-only (a policy cannot go below 100). For them the site
  summary adds SustainableFreedMB: the same scenario with the count limit raised to 100.
"""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from spruce_cli.common import UserError, timestamp, write_csv

POLICY_MINIMUM = 100
LEGACY_CUTOFF = datetime(2023, 1, 1, tzinfo=UTC)
GROUP = ["web_id", "doc_id"]


@dataclass(frozen=True)
class Scenario:
    count: int
    days: int
    cutoff: datetime | None
    hidden: bool

    @property
    def name(self) -> str:
        return f"count{self.count}_age{self.days}d"

    @property
    def trim_only(self) -> bool:
        return 0 < self.count < POLICY_MINIMUM


def build_scenarios(counts: list[int], days_list: list[int], as_of: datetime) -> list[Scenario]:
    def make(count: int, days: int, hidden: bool) -> Scenario:
        cutoff = as_of - timedelta(days=days) if days > 0 else None
        return Scenario(count=count, days=days, cutoff=cutoff, hidden=hidden)

    scenarios = [make(c, d, False) for c in counts for d in days_list if not (c == 0 and d == 0)]
    if not scenarios:
        raise UserError("Define at least one scenario with a count or an age limit.")
    names = {s.name for s in scenarios}
    for scenario in [s for s in scenarios if s.trim_only]:
        sustainable = make(POLICY_MINIMUM, scenario.days, True)
        if sustainable.name not in names:
            scenarios.append(sustainable)
            names.add(sustainable.name)
    return scenarios


def load_versions(path: Path) -> pl.DataFrame:
    """Read a version storage report, expand compact columns and rank versions per file."""
    raw = pl.read_csv(path, infer_schema=False)
    compact = [c for c in raw.columns if c.endswith(".Compact")]
    # Compact columns are empty when the value repeats the previous row
    raw = raw.with_columns([pl.col(c).forward_fill() for c in compact])
    return (
        raw.select(
            web_id=pl.col("WebId.Compact"),
            doc_id=pl.col("DocId.Compact"),
            web_url=pl.col("WebUrl.Compact"),
            file_url=pl.col("FileUrl.Compact"),
            major=pl.col("MajorVersion").cast(pl.Int64),
            minor=pl.col("MinorVersion").cast(pl.Int64),
            size=pl.col("Size").cast(pl.Int64),
            snapshot=pl.col("SnapshotDate").str.to_datetime(time_zone="UTC", strict=False),
        )
        .sort(
            ["web_id", "doc_id", "major", "minor"],
            descending=[False, False, True, True],
        )
        .with_columns(
            position=pl.int_range(pl.len()).over(GROUP),
            is_major=(pl.col("minor") == 0).cast(pl.Int64),
        )
        .with_columns(
            is_current=pl.col("position") == 0,
            newer_majors=pl.col("is_major").cum_sum().over(GROUP) - pl.col("is_major"),
        )
    )


def freed_expr(scenario: Scenario) -> pl.Expr:
    deleted = pl.lit(False)
    if scenario.count > 0:
        deleted = deleted | (pl.col("newer_majors") >= scenario.count)
    if scenario.cutoff is not None:
        deleted = deleted | (
            pl.col("snapshot").is_not_null()
            & ((pl.col("snapshot") < scenario.cutoff) | (pl.col("snapshot") < LEGACY_CUTOFF))
        )
    deleted = deleted & ~pl.col("is_current")
    return pl.when(deleted).then(pl.col("size")).otherwise(0).alias(f"freed_{scenario.name}")


def mb(value: float) -> str:
    return f"{value / (1024 * 1024):.2f}"


def read_site_map(jobs: Path) -> dict[str, str]:
    """Report file name -> site URL, from the session's version-report-jobs.csv."""
    if not jobs.exists():
        return {}
    with jobs.open(newline="", encoding="utf-8-sig") as handle:
        return {
            Path(r["LocalPath"].replace("\\", "/")).name: r["SiteUrl"]
            for r in csv.DictReader(handle)
            if r.get("LocalPath")
        }


def read_plan(plan: Path | None) -> dict[str, float]:
    if plan is None:
        return {}
    with plan.open(newline="", encoding="utf-8-sig") as handle:
        return {r["Url"]: float(r["ExcessMB"] or 0) for r in csv.DictReader(handle)}


def int_list(value: str) -> list[int]:
    return [int(v) for v in value.split(",") if v.strip()]


def run(
    report_path: Path,
    out_dir: Path,
    jobs_csv: Path,
    *,
    counts: list[int],
    days_list: list[int],
    as_of: datetime | None,
    plan: Path | None,
    include_file_detail: bool,
) -> None:
    if any(d != 0 and d < 30 for d in days_list):
        raise UserError("--expire-after-days values must be 0 (disabled) or at least 30.")
    if any(c < 0 for c in counts):
        raise UserError("--major-version-limit values must be 0 (disabled) or positive.")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    scenarios = build_scenarios(counts, days_list, as_of)

    report_files = sorted(report_path.glob("*.csv")) if report_path.is_dir() else [report_path]
    if not report_files or not report_files[0].exists():
        raise UserError(f"No report CSV files found at {report_path}")

    site_map = read_site_map(jobs_csv)
    excess_by_site = read_plan(plan)
    site_rows: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []

    for report in report_files:
        print(f"Analyzing {report.name}...")
        versions = load_versions(report).with_columns([freed_expr(s) for s in scenarios])
        if versions.is_empty():
            print(f"WARNING: {report.name} has no rows")
            continue
        site_url = site_map.get(report.name) or str(versions["web_url"][0])

        totals = versions.select(
            files=pl.struct(GROUP).n_unique(),
            versions=pl.len(),
            total=pl.col("size").sum(),
            current=pl.col("size").filter(pl.col("is_current")).sum(),
            **{f"freed_{s.name}": pl.col(f"freed_{s.name}").sum() for s in scenarios},
        ).row(0, named=True)

        excess_mb = excess_by_site.get(site_url)
        for scenario in scenarios:
            if scenario.hidden:
                continue
            freed = totals[f"freed_{scenario.name}"]
            sustainable = totals[f"freed_count{POLICY_MINIMUM}_age{scenario.days}d"] if scenario.trim_only else freed
            covers = covers_later = ""
            if excess_mb:
                covers = "Yes" if freed / 1024**2 >= excess_mb else "No"
                covers_later = "Yes" if sustainable / 1024**2 >= excess_mb else "No"
            site_rows.append(
                {
                    "SiteUrl": site_url,
                    "ReportFile": report.name,
                    "Scenario": scenario.name,
                    "MajorVersionLimit": scenario.count,
                    "ExpireAfterDays": scenario.days,
                    "TrimOnly": "Yes" if scenario.trim_only else "No",
                    "Files": totals["files"],
                    "Versions": totals["versions"],
                    "TotalMB": mb(totals["total"]),
                    "CurrentMB": mb(totals["current"]),
                    "HistoricalMB": mb(totals["total"] - totals["current"]),
                    "FreedMB": mb(freed),
                    "SustainableFreedMB": mb(sustainable),
                    "TemporaryFreedMB": mb(freed - sustainable),
                    "FreedPercent": f"{100 * freed / totals['total']:.1f}" if totals["total"] else "0.0",
                    "PlanExcessMB": excess_mb if excess_mb is not None else "",
                    "CoversExcess": covers,
                    "CoversExcessLater": covers_later,
                }
            )

        if include_file_detail:
            visible = [s for s in scenarios if not s.hidden]
            per_file = (
                versions.group_by(GROUP)
                .agg(
                    web_url=pl.col("web_url").first(),
                    file_url=pl.col("file_url").first(),
                    version_count=pl.len(),
                    current=pl.col("size").filter(pl.col("is_current")).sum(),
                    total=pl.col("size").sum(),
                    oldest=pl.col("snapshot").filter(~pl.col("is_current")).min(),
                    newest=pl.col("snapshot").filter(~pl.col("is_current")).max(),
                    **{f"freed_{s.name}": pl.col(f"freed_{s.name}").sum() for s in visible},
                )
                .filter(pl.col("version_count") > 1)
                .sort("total", descending=True)
            )
            for row in per_file.iter_rows(named=True):
                detail: dict[str, Any] = {
                    "SiteUrl": site_url,
                    "WebUrl": row["web_url"],
                    "FileUrl": row["file_url"],
                    "VersionCount": row["version_count"],
                    "CurrentMB": mb(row["current"]),
                    "HistoricalMB": mb(row["total"] - row["current"]),
                    "OldestSnapshot": row["oldest"].date().isoformat() if row["oldest"] else "",
                    "NewestSnapshot": row["newest"].date().isoformat() if row["newest"] else "",
                }
                for s in visible:
                    detail[f"FreedMB_{s.name}"] = mb(row[f"freed_{s.name}"])
                file_rows.append(detail)

    stamp = timestamp()
    write_csv(out_dir / f"version-whatif-sites_{stamp}.csv", site_rows)
    if include_file_detail:
        write_csv(out_dir / f"version-whatif-files_{stamp}.csv", file_rows)

    for r in site_rows:
        print(
            f"{r['SiteUrl']}  {r['Scenario']:<18} trimOnly={r['TrimOnly']:<3} "
            f"freed={r['FreedMB']} MB  sustainable={r['SustainableFreedMB']} MB  "
            f"covers={r['CoversExcess'] or '-'}/{r['CoversExcessLater'] or '-'}"
        )
