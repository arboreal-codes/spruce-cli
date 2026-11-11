"""Site storage export (step 1), quota plan (step 2) and quota apply (step 3)."""

import math
import re
from pathlib import Path
from typing import Any

from spruce_cli.common import (
    UserError,
    append_csv_row,
    mb_to_gb,
    read_csv,
    timestamp,
    write_csv,
)
from spruce_cli.sharepoint import SharePoint, SharePointError
from spruce_cli.strategies import STRATEGIES, QuotaOptions, whole_gb_in_mb


def _csom_date(value: object) -> str:
    """/Date(2023,5,14,7,30,33,573)/ (zero-based month) -> 2023-06-14"""
    match = re.search(r"Date\((\d+),(\d+),(\d+)", str(value or ""))
    if not match:
        return str(value or "")[:10]
    year, month, day = (int(g) for g in match.groups())
    return f"{year}-{month + 1:02d}-{day:02d}"


def _guid(value: object) -> str:
    match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", str(value or ""))
    return match.group(0) if match else ""


def export_sites(sp: SharePoint, session: Path) -> Path:
    """One row per site with usage and quota. Excludes OneDrive and redirect sites."""
    print("Reading tenant sites...")
    rows: list[dict[str, Any]] = []
    for site in sp.list_sites():
        url = str(site.get("Url", "")).rstrip("/")
        template = str(site.get("Template", ""))
        if template.startswith(("RedirectSite", "SPSPERS")) or "-my.sharepoint.com" in url:
            continue
        used = int(site.get("StorageUsage") or 0)
        rows.append(
            {
                "Url": url,
                "Title": site.get("Title", ""),
                "Template": template,
                "GroupId": _guid(site.get("GroupId")),
                "LockState": site.get("LockState", ""),
                "LastContentModifiedDate": _csom_date(site.get("LastContentModifiedDate")),
                "UsedMB": used,
                "UsedGB": mb_to_gb(used),
                "QuotaMB": int(site.get("StorageMaximumLevel") or 0),
                "WarningMB": int(site.get("StorageWarningLevel") or 0),
            }
        )
    rows.sort(key=lambda r: -int(r["UsedMB"]))
    out = session / f"site-storage_{timestamp()}.csv"
    write_csv(out, rows)
    print(f"Sites: {len(rows)}  Total used: {mb_to_gb(sum(int(r['UsedMB']) for r in rows))} GB")
    return out


def build_plan(
    sites: list[dict[str, str]],
    fixed: dict[str, int],
    options: QuotaOptions,
    strategy: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return plan rows and summary rows."""
    if not 0 <= options.reserve_percent < 90:
        raise UserError("--reserve-percent must be between 0 and 90")
    if not 50 <= options.warning_percent <= 99:
        raise UserError("--warning-percent must be between 50 and 99")
    if not 0.5 <= options.freeze_margin_percent <= 50:
        raise UserError("--freeze-margin-percent must be between 0.5 and 50")

    budget_mb = options.budget_tb * 1024 * 1024
    assignable_mb = budget_mb * (1 - options.reserve_percent / 100)
    fixed_total_mb = sum(fixed[s["Url"]] for s in sites if s["Url"] in fixed)
    pool = [s for s in sites if s["Url"] not in fixed]
    pool_used = [int(float(s["UsedMB"])) for s in pool]
    try:
        cap, allocations = (
            STRATEGIES[strategy](pool_used, assignable_mb - fixed_total_mb, options) if pool else (None, [])
        )
    except ValueError as error:
        raise UserError(str(error)) from error
    by_url = {s["Url"]: a for s, a in zip(pool, allocations, strict=True)}

    rows: list[dict[str, Any]] = []
    for site in sites:
        used_mb = int(float(site["UsedMB"]))
        if site["Url"] in fixed:
            quota_mb = target_mb = fixed[site["Url"]]
            status = "FixedBelowUsage" if quota_mb <= used_mb else "Fixed"
            excess_mb = 0
        else:
            allocation = by_url[site["Url"]]
            status, quota_mb, target_mb = (
                allocation.status,
                allocation.quota_mb,
                allocation.target_mb,
            )
            excess_mb = allocation.excess_mb(used_mb)
        rows.append(
            {
                "Url": site["Url"],
                "Title": site.get("Title", ""),
                "Status": status,
                "UsedMB": used_mb,
                "UsedGB": mb_to_gb(used_mb),
                "ProposedQuotaMB": quota_mb,
                "ProposedQuotaGB": mb_to_gb(quota_mb),
                "ProposedWarningMB": math.floor(quota_mb * options.warning_percent / 100),
                "TargetQuotaMB": target_mb,
                "ExcessMB": excess_mb,
                "ExcessGB": mb_to_gb(excess_mb),
                "CurrentQuotaMB": site.get("QuotaMB", ""),
            }
        )
    rows.sort(key=lambda r: (-int(r["ExcessMB"]), -int(r["UsedMB"])))

    sum_proposed_mb = sum(int(r["ProposedQuotaMB"]) for r in rows)
    over_cap = [r for r in rows if r["Status"] == "OverCap"]
    summary: dict[str, Any] = {
        "Strategy": strategy,
        "BudgetGB": mb_to_gb(budget_mb),
        "ReservePercent": options.reserve_percent,
        "GrowthMarginPercent": options.growth_margin_percent,
        "AssignableGB": mb_to_gb(assignable_mb),
        "FixedQuotaGB": mb_to_gb(fixed_total_mb),
        "TotalUsedGB": mb_to_gb(sum(int(r["UsedMB"]) for r in rows)),
        "CapGB": mb_to_gb(cap) if cap is not None else "",
        "SitesTotal": len(rows),
        "SitesOverCap": len(over_cap),
        "TotalExcessGB": mb_to_gb(sum(int(r["ExcessMB"]) for r in over_cap)),
        "SumProposedQuotaGB": mb_to_gb(sum_proposed_mb),
        "QuotaToBudgetRatio": f"{sum_proposed_mb / budget_mb:.2f}",
    }
    return rows, summary


def plan_quotas(
    session: Path,
    storage_csv: Path,
    options: QuotaOptions,
    strategy: str,
    fixed_csv: Path | None,
) -> Path:
    sites = read_csv(storage_csv)
    if not sites:
        raise UserError(f"No sites found in {storage_csv}")
    fixed = {}
    if fixed_csv:
        fixed = {r["Url"].rstrip("/"): whole_gb_in_mb(float(r["QuotaGB"]) * 1024) for r in read_csv(fixed_csv)}
    rows, summary = build_plan(sites, fixed, options, strategy)
    summary = {"SiteStorageCsv": str(storage_csv.resolve()), **summary}

    # Strategy in the file name so plans from different strategies sit side by side
    stamp = f"{timestamp()}_{strategy}"
    plan = session / f"quota-plan_{stamp}.csv"
    write_csv(plan, rows)
    write_csv(session / f"quota-plan-summary_{stamp}.csv", [summary])
    for key, value in summary.items():
        print(f"{key:<20} {value}")
    if any(r["Status"] == "FixedBelowUsage" for r in rows):
        print("WARNING: some fixed quotas are at or below current usage; applying them would lock those sites.")
    return plan


def apply_quotas(sp: SharePoint, session: Path, plan: Path, over_cap_mode: str, dry_run: bool) -> None:
    """Apply plan quotas. Re-reads usage first and never sets a quota at or below it (read-only)."""
    print(f"Plan: {plan}")
    log = session / f"quota-apply-log_{timestamp()}.csv"
    counts: dict[str, int] = {}
    for row in read_csv(plan):
        url, status = row["Url"], row["Status"]
        quota_mb, warning_mb = (
            int(row["ProposedQuotaMB"]),
            int(row["ProposedWarningMB"]),
        )
        used_now: Any = ""
        message = ""
        if status == "OverCap" and over_cap_mode == "skip":
            action, message = "Skipped", "OverCap site skipped (--over-cap-mode skip)"
        else:
            try:
                used_now = int(sp.site_properties(url)["StorageUsage"])
                if quota_mb <= used_now:
                    action = "Skipped"
                    message = f"Quota would be at or below current usage ({used_now} MB) and lock the site"
                elif dry_run:
                    action, message = (
                        "WhatIf",
                        f"Would set quota to {quota_mb} MB (warning at {warning_mb} MB)",
                    )
                else:
                    sp.set_site_properties(
                        url,
                        [
                            ("StorageMaximumLevel", "Int64", quota_mb),
                            ("StorageWarningLevel", "Int64", warning_mb),
                        ],
                    )
                    action = "Applied"
            except SharePointError as error:
                action, message = "Failed", str(error)
        counts[action] = counts.get(action, 0) + 1
        print(f"{action:<8} {url}  {message}")
        append_csv_row(
            log,
            {
                "Url": url,
                "PlanStatus": status,
                "QuotaMB": quota_mb,
                "WarningMB": warning_mb,
                "CurrentUsedMB": used_now,
                "Action": action,
                "Message": message,
            },
        )
    print(f"Log: {log}")
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
