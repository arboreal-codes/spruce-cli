from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from spruce_cli.common import UserError
from spruce_cli.targets import SiteFilter, select_sites
from spruce_cli.versions import pending_reports

OLD = (date.today() - timedelta(days=400)).isoformat()
NEW = date.today().isoformat()
BASE = "https://t.sharepoint.com/sites"


def make_session(root: Path, name: str) -> Path:
    session = root / name
    session.mkdir(parents=True)
    (session / "session.json").write_text("{}")
    return session


@pytest.fixture
def session(tmp_path: Path) -> Path:
    session = make_session(tmp_path, "20261110-000000_now")
    (session / "site-storage_20261110-000000.csv").write_text(
        "Url,Title,Template,LastContentModifiedDate,UsedMB\n"
        f"{BASE}/big,Big,GROUP#0,{NEW},{200 * 1024}\n"
        f"{BASE}/mid,Proyectos,STS#3,{OLD},{90 * 1024}\n"
        f"{BASE}/small,Small,GROUP#0,{OLD},{5 * 1024}\n"
    )
    (session / "quota-plan_20261110-000001_growth-margin.csv").write_text(
        f"Url,Status,UsedMB\n{BASE}/big,OverCap,{200 * 1024}\n{BASE}/mid,UnderCap,{90 * 1024}\n"
        f"{BASE}/small,UnderCap,{5 * 1024}\n"
    )
    return session


def pick(session: Path, command: str = "cleanup", **kwargs: Any) -> list[str]:
    urls = select_sites(session, session.parent, SiteFilter(**kwargs), command)
    return [u.rsplit("/", 1)[-1] for u in urls]


def test_filters(session: Path) -> None:
    assert pick(session, min_used_gb=80) == ["big", "mid"]
    assert pick(session, status="UnderCap") == ["mid", "small"]
    assert pick(session, inactive_days=365) == ["mid", "small"]
    assert pick(session, template=["GROUP"]) == ["big", "small"]
    assert pick(session, match=["*proyectos*"]) == ["mid"]
    assert pick(session, all_sites=True, top=1) == ["big"]


def test_requires_explicit_criteria(session: Path) -> None:
    with pytest.raises(UserError):
        pick(session)


def test_exclusions_and_skip_done(session: Path, tmp_path: Path) -> None:
    exclude_file = tmp_path / "skip.txt"
    exclude_file.write_text(f"# comment\n{BASE}/small/\n")
    assert pick(session, all_sites=True, exclude=["*big*"], exclude_file=[exclude_file]) == ["mid"]

    old = make_session(tmp_path, "20261101-000000_old")
    (old / "cleanup-summary_20261101-000000.csv").write_text(
        f"SiteUrl,Message\n{BASE}/big,\n{BASE}/mid,recycle bin: 403\n"
    )
    assert pick(session, all_sites=True, skip_done=True) == ["mid", "small"]  # failed analysis is not done
    assert pick(session, all_sites=True, skip_done=True, command="report") == ["big", "mid", "small"]


def test_pending_reports_across_sessions(tmp_path: Path) -> None:
    old = make_session(tmp_path, "20261101-000000_old")
    (old / "version-report-jobs.csv").write_text(
        "SiteUrl,RequestedAt,Status\n"
        f"{BASE}/A,2026-11-01,InProgress\n{BASE}/b,2026-11-01,Downloaded\n{BASE}/c,2026-11-01,Failed\n"
    )
    assert pending_reports(tmp_path) == {f"{BASE}/a": ("20261101-000000_old", "2026-11-01")}
