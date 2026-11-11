"""Space held outside current files: recycle bins and the Preservation Hold Library.

Both count toward a site's storage but are not part of the version reports:
- Recycle bin, stage 1 (end-user bin) and stage 2 (site collection bin). Items leave
  automatically 93 days after the first deletion; purging them frees space at once.
- Preservation Hold Library: copies kept by retention policies / holds. It can only be
  freed by changing or excluding the site from the retention policy (Purview), never by
  deleting items here, so it is reported, not purged.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from spruce_cli.common import (
    append_csv_row,
    bytes_to_mb,
    confirm,
    iso_now,
    timestamp,
    write_csv,
)
from spruce_cli.sharepoint import (
    SharePoint,
    SharePointError,
    odata_string,
    server_relative,
)

RECYCLE_FIELDS = "Id,Title,LeafName,DirName,ItemState,ItemType,Size,DeletedDate,DeletedByEmail,AuthorEmail"
PAGE_SIZE = 5000
STAGES = {1: "FirstStage", 2: "SecondStage"}


def _parse_date(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def recycle_bin_items(sp: SharePoint, site: str) -> tuple[list[dict[str, Any]], bool]:
    """All recycle bin items of the site collection (both stages). Returns (items, complete)."""
    try:
        return sp.get_all(f"{site}/_api/site/RecycleBin?$select={RECYCLE_FIELDS}"), True
    except SharePointError as error:
        print(f"  Full recycle bin read failed ({error.status}), paging instead...")
    # ponytail: pagingInfo format (id + title) mirrors PnP; the seen-id guard stops a loop if the tenant ignores it
    items: dict[str, dict[str, Any]] = {}
    paging = ""
    for state in (1, 2):
        paging = ""
        while True:
            url = (
                f"{site}/_api/site/GetRecycleBinItems(pagingInfo=@p,rowLimit={PAGE_SIZE},isAscending=true,"
                f"orderBy=0,itemState={state})?@p={odata_string(paging)}&$select={RECYCLE_FIELDS}"
            )
            page = sp.get(url).get("value", [])
            new = [i for i in page if i["Id"] not in items]
            items.update({i["Id"]: i for i in new})
            if len(page) < PAGE_SIZE:
                break
            if not new:
                return list(items.values()), False
            last = page[-1]
            paging = f"id={last['Id']}&title={quote(str(last.get('Title', '')))}"
    return list(items.values()), True


def preservation_hold(sp: SharePoint, site: str) -> dict[str, Any] | None:
    """StorageMetrics of the root web's Preservation Hold Library, or None if the site has none."""
    # ponytail: root web only; subsites have their own PHL, add a web walk if retention covers subsites
    folder = f"{server_relative(site).rstrip('/')}/PreservationHoldLibrary"
    try:
        return sp.get(
            f"{site}/_api/web/GetFolderByServerRelativePath(decodedurl=@p)/StorageMetrics?@p={odata_string(folder)}"
        )
    except SharePointError as error:
        if error.status in (404, 400):
            return None
        raise


def preservation_hold_files(sp: SharePoint, site: str) -> list[dict[str, Any]]:
    library = f"{server_relative(site).rstrip('/')}/PreservationHoldLibrary"
    return sp.get_all(
        f"{site}/_api/web/GetList(@p)/items?@p={odata_string(library)}"
        f"&$select=FileRef,Created,Modified,File/Length&$expand=File&$top={PAGE_SIZE}"
    )


def analyze(sp: SharePoint, session: Path, sites: list[str], detail: bool) -> Path:
    now = datetime.now(UTC)
    summary_rows: list[dict[str, Any]] = []
    recycle_rows: list[dict[str, Any]] = []
    hold_rows: list[dict[str, Any]] = []

    for site in sites:
        print(f"Analyzing {site}...")
        row: dict[str, Any] = {"SiteUrl": site}
        messages: list[str] = []
        try:
            used_mb = float(sp.site_properties(site)["StorageUsage"])
        except SharePointError as error:
            used_mb = 0.0
            messages.append(f"usage: {error}")
        row["UsedMB"] = int(used_mb)

        try:
            items, complete = recycle_bin_items(sp, site)
            if not complete:
                messages.append("recycle bin paging stopped early; figures are a lower bound")
        except SharePointError as error:
            items = []
            messages.append(f"recycle bin: {error}")
        reclaimable = 0.0
        for state, label in STAGES.items():
            in_stage = [i for i in items if int(i.get("ItemState") or 0) == state]
            size = sum(float(i.get("Size") or 0) for i in in_stage)
            old = sum(
                float(i.get("Size") or 0)
                for i in in_stage
                if (d := _parse_date(i.get("DeletedDate"))) and d < now - timedelta(days=30)
            )
            reclaimable += size
            row[f"Recycle{label}Items"] = len(in_stage)
            row[f"Recycle{label}MB"] = bytes_to_mb(size)
            row[f"Recycle{label}Older30dMB"] = bytes_to_mb(old)
        dates = [d for i in items if (d := _parse_date(i.get("DeletedDate")))]
        row["RecycleOldestDeleted"] = min(dates).date().isoformat() if dates else ""

        try:
            metrics = preservation_hold(sp, site)
        except SharePointError as error:
            metrics = None
            messages.append(f"preservation hold: {error}")
        hold_size = float(metrics.get("TotalSize") or 0) if metrics else 0.0
        row["PreservationHoldFiles"] = int(metrics.get("TotalFileCount") or 0) if metrics else 0
        row["PreservationHoldMB"] = bytes_to_mb(hold_size)
        row["PreservationHoldLastModified"] = str(metrics.get("LastModified") or "")[:10] if metrics else ""
        held_mb = (reclaimable + hold_size) / 1024**2
        row["HeldOutsideFilesMB"] = f"{held_mb:.2f}"
        row["HeldPercentOfUsed"] = f"{100 * held_mb / used_mb:.1f}" if used_mb else ""
        row["Message"] = "; ".join(messages)
        summary_rows.append(row)
        print(
            f"  recycle stage1={row['RecycleFirstStageMB']} MB stage2={row['RecycleSecondStageMB']} MB  "
            f"preservation hold={row['PreservationHoldMB']} MB  ({row['HeldPercentOfUsed'] or '-'}% of used)"
        )

        if detail:
            for i in items:
                recycle_rows.append(
                    {
                        "SiteUrl": site,
                        "Stage": STAGES.get(int(i.get("ItemState") or 0), i.get("ItemState")),
                        "Id": i.get("Id"),
                        "ItemType": i.get("ItemType"),
                        "Path": f"{i.get('DirName', '')}/{i.get('LeafName') or i.get('Title', '')}",
                        "SizeMB": bytes_to_mb(float(i.get("Size") or 0)),
                        "DeletedDate": i.get("DeletedDate"),
                        "DeletedBy": i.get("DeletedByEmail"),
                    }
                )
            if metrics and row["PreservationHoldFiles"]:
                try:
                    for f in preservation_hold_files(sp, site):
                        hold_rows.append(
                            {
                                "SiteUrl": site,
                                "FileRef": f.get("FileRef"),
                                "SizeMB": bytes_to_mb(float((f.get("File") or {}).get("Length") or 0)),
                                "Created": f.get("Created"),
                                "Modified": f.get("Modified"),
                            }
                        )
                except SharePointError as error:
                    print(f"  could not list preservation hold files: {error}")

    stamp = timestamp()
    out = session / f"cleanup-summary_{stamp}.csv"
    write_csv(out, summary_rows)
    if detail:
        write_csv(
            session / f"recycle-bin-items_{stamp}.csv",
            sorted(recycle_rows, key=lambda r: -float(str(r["SizeMB"]))),
        )
        write_csv(
            session / f"preservation-hold-files_{stamp}.csv",
            sorted(hold_rows, key=lambda r: -float(str(r["SizeMB"]))),
        )
    return out


def purge_recycle_bin(
    sp: SharePoint,
    session: Path,
    sites: list[str],
    *,
    stage: str,
    older_than_days: int,
    dry_run: bool,
    assume_yes: bool,
) -> None:
    """PERMANENTLY delete recycle bin items (by stage and age). Cannot be undone."""
    states = {"1": [1], "2": [2], "all": [1, 2]}[stage]
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    targets: dict[str, list[dict[str, Any]]] = {}
    for site in sites:
        items, _ = recycle_bin_items(sp, site)
        targets[site] = [
            i
            for i in items
            if int(i.get("ItemState") or 0) in states
            and (older_than_days == 0 or ((d := _parse_date(i.get("DeletedDate"))) is not None and d < cutoff))
        ]
        size = sum(float(i.get("Size") or 0) for i in targets[site])
        print(f"{site}: {len(targets[site])} items, {bytes_to_mb(size)} MB")
    total = sum(len(v) for v in targets.values())
    if dry_run or total == 0:
        print("Dry run, nothing deleted." if dry_run else "Nothing to delete.")
        return
    if not assume_yes:
        confirm(
            f"PERMANENTLY delete {total} recycle bin item(s)? This cannot be undone.",
            "delete",
        )

    log = session / "recycle-bin-purge-log.csv"
    for site, items in targets.items():
        ids = [i["Id"] for i in items]
        deleted, message = 0, ""
        try:
            for start in range(0, len(ids), 200):
                chunk = ids[start : start + 200]
                sp.request(
                    "POST",
                    f"{site}/_api/site/RecycleBin/DeleteByIds",
                    json={"ids": chunk},
                    headers={"Content-Type": "application/json;odata=nometadata"},
                )
                deleted += len(chunk)
            action = "Deleted"
        except SharePointError as error:
            action, message = "Failed", str(error)
        size = sum(float(i.get("Size") or 0) for i in items[:deleted])
        print(f"{action:<8} {site}  {deleted}/{len(ids)} items  {bytes_to_mb(size)} MB  {message}")
        append_csv_row(
            log,
            {
                "SiteUrl": site,
                "Stage": stage,
                "OlderThanDays": older_than_days,
                "RequestedAt": iso_now(),
                "Items": deleted,
                "FreedMB": bytes_to_mb(size),
                "Action": action,
                "Message": message,
            },
        )
    print(f"Log: {log}")
