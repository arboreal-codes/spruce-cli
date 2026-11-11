"""Version storage reports (step 4), version policy and trim jobs (step 6)."""

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from spruce_cli.common import (
    HOME_DIR,
    UserError,
    append_csv_row,
    confirm,
    iso_now,
    read_csv,
    slug,
    timestamp,
    write_csv,
)
from spruce_cli.sharepoint import (
    SharePoint,
    SharePointError,
    odata_string,
    origin,
    parse_result,
    server_relative,
)

POLICY_MINIMUM = 100
JOBS_FILE = "version-report-jobs.csv"
REPORTS_DIR = "version-reports"


# ---------------------------------------------------------------- reports


def _ensure_library(sp: SharePoint, site: str, library: str) -> None:
    try:
        sp.get(f"{site}/_api/web/lists/GetByTitle({odata_string(library)})?$select=Id")
    except SharePointError as error:
        if error.status != 404:
            raise
        sp.post(
            f"{site}/_api/web/lists",
            {
                "__metadata": {"type": "SP.List"},
                "BaseTemplate": 101,
                "Title": library,
                "Hidden": True,
            },
        )


def pending_reports(sessions_dir: Path) -> dict[str, tuple[str, str]]:
    """Site URL (lowercase) -> (session, RequestedAt) for report jobs still Requested or InProgress in any session."""
    pending: dict[str, tuple[str, str]] = {}
    for path in sorted(sessions_dir.glob(f"*/{JOBS_FILE}")):
        for row in read_csv(path):
            if row.get("Status") in ("Requested", "InProgress"):
                pending[row["SiteUrl"].rstrip("/").lower()] = (path.parent.name, row.get("RequestedAt", ""))
    return pending


def start_reports(
    sp: SharePoint, session: Path, sessions_dir: Path, sites: list[str], library: str, dry_run: bool, force: bool
) -> None:
    """Queue a version storage report job per site. Takes ~1 to several days per site.

    Sites with a job still Requested/InProgress in any session are skipped (unless force):
    a new request while one runs supersedes it and the earlier wait is lost. Run
    report-receive in that session to refresh its status.
    """
    jobs = session / JOBS_FILE
    stamp = timestamp()
    pending = {} if force else pending_reports(sessions_dir)
    for site in sites:
        report_url = f"{site}/{library}/version-report_{stamp}.csv"
        if (running := pending.get(site.lower())) is not None:
            print(f"Running    {site}  already requested {running[1]} in session {running[0]}; skipped")
            continue
        if dry_run:
            print(f"WhatIf     {site}  would queue report into {report_url}")
            continue
        try:
            _ensure_library(sp, site, library)
            sp.post(
                f"{site}/_api/site/StartFileVersionExpirationReport",
                {"reportFileUrl": report_url},
            )
            status, message = "Requested", ""
        except SharePointError as error:
            status, message = "Failed", str(error)
        print(f"{status:<10} {site}  {message}")
        append_csv_row(
            jobs,
            {
                "SiteUrl": site,
                "ReportUrl": report_url,
                "RequestedAt": iso_now(),
                "Status": status,
                "LocalPath": "",
                "Message": message,
            },
        )
    print(f"Job list: {jobs}")


def receive_reports(sp: SharePoint, session: Path) -> None:
    """Check pending report jobs and download finished reports. Safe to run repeatedly."""
    jobs_csv = session / JOBS_FILE
    if not jobs_csv.exists():
        raise UserError(f"No {JOBS_FILE} in session {session.name}. Run: spruce versions report-start")
    report_dir = session / REPORTS_DIR
    report_dir.mkdir(exist_ok=True)
    jobs = read_csv(jobs_csv)
    for job in jobs:
        if job["Status"] not in ("Requested", "InProgress"):
            continue
        site, report_url = job["SiteUrl"], job["ReportUrl"]
        try:
            progress = parse_result(
                sp.post(
                    f"{site}/_api/site/GetProgressForFileVersionExpirationReport",
                    {"reportFileUrl": report_url},
                )
            )
            state = str(progress.get("status") or progress.get("Status") or progress.get("raw") or "unknown")
            if state == "completed":
                relative = server_relative(report_url)
                target = report_dir / f"{slug(site)}__{relative.rsplit('/', 1)[-1]}"
                sp.download(
                    f"{site}/_api/web/GetFileByServerRelativePath(decodedurl=@p)/$value?@p={odata_string(relative)}",
                    target,
                )
                job.update(Status="Downloaded", LocalPath=str(target.resolve()), Message="")
            elif "fail" in state or "no_report" in state:
                job.update(Status="Failed", Message=f"Job state: {state}")
            else:
                job.update(Status="InProgress", Message=f"Job state: {state}")
        except SharePointError as error:
            job["Message"] = f"Could not read job progress: {error}"
        print(f"{job['Status']:<10} {site}  {job['Message']}")
    write_csv(jobs_csv, list(jobs))


# ---------------------------------------------------------------- policy


def set_policy(
    sp: SharePoint,
    session: Path,
    sites: list[str],
    *,
    major_limit: int,
    expire_days: int,
    minor_limit: int,
    automatic: bool,
    apply_existing: bool,
    dry_run: bool,
    assume_yes: bool,
) -> None:
    """Version history limits for new (and optionally existing) libraries. Deletes nothing."""
    if automatic:
        description = "Automatic version limits"
        expire_value = major_value = minor_value = -1
    else:
        if not POLICY_MINIMUM <= major_limit <= 50000:
            raise UserError(
                "--major-version-limit must be between 100 and 50000 (use trim for one-time trims below 100)"
            )
        if expire_days != 0 and not 30 <= expire_days <= 36500:
            raise UserError("--expire-after-days must be 0 or between 30 and 36500")
        if not 0 <= minor_limit <= major_limit:
            raise UserError("--major-with-minor-limit must be between 0 and --major-version-limit")
        description = f"Manual limits: {major_limit} major versions, expire after {expire_days} days (0 = never)"
        expire_value, major_value = expire_days, major_limit
        minor_value = minor_limit if apply_existing else -1
    scope = "new and existing libraries" if apply_existing else "new libraries only"
    print(f"{description}; {scope}\nSites: {len(sites)}")
    if not dry_run and not assume_yes:
        confirm(f"Apply this version policy to {len(sites)} site(s)?", "yes")

    log = session / f"version-policy-log_{timestamp()}.csv"
    for site in sites:
        message = ""
        if dry_run:
            action = "WhatIf"
        else:
            try:
                sp.set_site_properties(
                    site,
                    [
                        ("InheritVersionPolicyFromTenant", "Boolean", False),
                        ("EnableAutoExpirationVersionTrim", "Boolean", automatic),
                        ("ApplyToNewDocumentLibraries", "Boolean", True),
                        ("ApplyToExistingDocumentLibraries", "Boolean", apply_existing),
                        ("ExpireVersionsAfterDays", "Int32", expire_value),
                        ("MajorVersionLimit", "Int32", major_value),
                        ("MajorWithMinorVersionsLimit", "Int32", minor_value),
                    ],
                )
                action = "Applied"
            except SharePointError as error:
                action, message = "Failed", str(error)
        print(f"{action:<8} {site}  {message}")
        append_csv_row(
            log,
            {
                "SiteUrl": site,
                "Policy": description,
                "ExistingLibraries": apply_existing,
                "Action": action,
                "Message": message,
            },
        )
    print(f"Log: {log}")


# ---------------------------------------------------------------- trim

TRIM_MODES = {
    "count": "CountLimits",
    "age": "DeleteOlderThanDays",
    "automatic": "AutomaticTrim",
}


def _trim_metadata(sp: SharePoint, site: str, mode_name: str) -> tuple[int, str]:
    """Numeric value of FileVersionBatchDeleteMode.<mode> and its namespace, from the tenant's $metadata."""
    cache = HOME_DIR / "sp-rest-metadata.xml"
    if not cache.exists():
        print("Downloading SharePoint REST metadata (one time)...")
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(
            sp.request(
                "GET",
                f"{origin(site)}/_api/$metadata",
                headers={"Accept": "application/xml"},
            ).content
        )
    root = ET.parse(cache).getroot()
    value, namespace = None, None
    for schema in root.iter():
        if not schema.tag.endswith("Schema"):
            continue
        for child in schema:
            name = child.get("Name")
            if child.tag.endswith("EnumType") and name == "FileVersionBatchDeleteMode":
                for member in child:
                    if member.get("Name") == mode_name:
                        value = int(member.get("Value", ""))
            if child.tag.endswith("ComplexType") and name == "FileVersionBatchDeleteParameters":
                namespace = schema.get("Namespace")
    if value is None or not namespace:
        raise UserError(f"Could not resolve FileVersionBatchDeleteMode.{mode_name}. Delete {cache} to refresh it.")
    return value, namespace


def trim_status(sp: SharePoint, sites: list[str]) -> None:
    for site in sites:
        print(f"== {site}")
        try:
            print(
                json.dumps(
                    parse_result(sp.post(f"{site}/_api/site/GetProgressForDeleteFileVersions")),
                    indent=2,
                )
            )
        except SharePointError as error:
            print(f"Could not read trim progress: {error}")


def trim_cancel(sp: SharePoint, sites: list[str]) -> None:
    for site in sites:
        try:
            sp.post(f"{site}/_api/site/CancelDeleteFileVersions")
            print(f"Cancelled  {site}")
        except SharePointError as error:
            print(f"Could not cancel the trim job on {site}: {error}")


def start_trim(
    sp: SharePoint,
    session: Path,
    sites: list[str],
    *,
    mode: str,
    major_limit: int,
    minor_limit: int,
    delete_days: int,
    allow_below: bool,
    dry_run: bool,
    assume_yes: bool,
) -> None:
    """Queue trim jobs. PERMANENT: versions bypass the recycle bin. Each job applies one mode."""
    below_minimum = False
    params: dict[str, Any] = {
        "DeleteOlderThanDays": -1,
        "MajorVersionLimit": -1,
        "MajorWithMinorVersionsLimit": -1,
    }
    if mode == "count":
        if major_limit < 1:
            raise UserError("--mode count requires --major-version-limit >= 1")
        if not 0 <= minor_limit <= major_limit:
            raise UserError("--major-with-minor-limit must be between 0 and --major-version-limit")
        if major_limit < POLICY_MINIMUM:
            if not allow_below:
                raise UserError(
                    f"--major-version-limit {major_limit} is below the policy minimum ({POLICY_MINIMUM}). "
                    "This is a one-time trim; versions grow back. Add --allow-below-policy-minimum to confirm."
                )
            below_minimum = True
        params.update(MajorVersionLimit=major_limit, MajorWithMinorVersionsLimit=minor_limit)
        description = f"PERMANENTLY delete versions beyond {major_limit} major versions"
    elif mode == "age":
        if delete_days < 30:
            raise UserError("--mode age requires --delete-before-days >= 30")
        params["DeleteOlderThanDays"] = delete_days
        description = f"PERMANENTLY delete versions older than {delete_days} days"
    else:
        description = "PERMANENTLY delete versions using the automatic algorithm"

    mode_name = TRIM_MODES[mode]
    mode_value, namespace = _trim_metadata(sp, sites[0], mode_name)
    body = {
        "batchDeleteParameters": {
            "__metadata": {"type": f"{namespace}.FileVersionBatchDeleteParameters"},
            "BatchDeleteMode": mode_value,
            **params,
        }
    }
    param_text = ";".join(f"{k}={v}" for k, v in params.items() if v != -1)
    if below_minimum:
        param_text += ";BelowPolicyMinimum=True"
        print(
            f"WARNING: one-time trim to {major_limit} versions; the policy (min {POLICY_MINIMUM}) lets them grow back."
        )

    print(f"{description}\nSites: {', '.join(sites)}")
    if not dry_run and not assume_yes:
        confirm("This cannot be undone.", "delete")

    log = session / "version-trim-log.csv"
    for site in sites:
        if dry_run:
            print(f"WhatIf  {site}  {param_text}")
            continue
        action, message = "Failed", ""
        try:
            sp.post(f"{site}/_api/site/StartDeleteFileVersionsByMode", body)
            action = "Queued"
            # Read the job back and cancel it if SharePoint registered something else
            time.sleep(3)
            progress = parse_result(sp.post(f"{site}/_api/site/GetProgressForDeleteFileVersions"))
            registered_mode = str(progress.get("BatchDeleteMode", "")).lower()
            registered_major = str(progress.get("MajorVersionLimit", ""))
            mismatch = "" if registered_mode == mode_name.lower() else f" mode '{registered_mode}'"
            if mode == "count" and registered_major != str(major_limit):
                mismatch += f" major limit '{registered_major}'"
            if mismatch:
                sp.post(f"{site}/_api/site/CancelDeleteFileVersions")
                action = "CancelledMismatch"
                message = f"SharePoint registered{mismatch} instead of the requested values; job cancelled."
            else:
                message = f"Verified: {registered_mode} {param_text}"
        except SharePointError as error:
            prefix = "Queued but could not verify it; check with --mode status. " if action == "Queued" else ""
            message = f"{prefix}{error}"
        print(f"{action:<8} {site}  {message}")
        append_csv_row(
            log,
            {
                "SiteUrl": site,
                "Mode": mode,
                "Parameters": param_text,
                "RequestedAt": iso_now(),
                "Action": action,
                "Message": message,
            },
        )
    print(f"Log: {log}")
