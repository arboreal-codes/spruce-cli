from pathlib import Path

import pytest

from spruce_cli.common import Config, UserError, create_session, read_csv, resolve_session
from spruce_cli.quota import _csom_date, build_plan, export_sites
from spruce_cli.sharepoint import odata_string, server_relative
from spruce_cli.strategies import STRATEGIES, QuotaOptions, water_fill_cap

GB = 1024
SITES = [
    {"Url": f"https://t.sharepoint.com/sites/s{i}", "UsedMB": str(u * GB)} for i, u in enumerate([5, 50, 200, 900])
]


def test_water_fill_cap_is_exact() -> None:
    sizes = [10.0, 20.0, 30.0, 100.0]
    cap = water_fill_cap(sizes, 100)
    assert cap is not None
    assert sum(min(s, cap) for s in sizes) == pytest.approx(100)
    assert water_fill_cap(sizes, 1000) is None


@pytest.mark.parametrize("strategy", sorted(STRATEGIES))
def test_strategies_never_lock_sites(strategy: str) -> None:
    rows, summary = build_plan(SITES, {}, QuotaOptions(budget_tb=0.5), strategy)
    assert summary["Strategy"] == strategy
    for row in rows:
        assert row["ProposedQuotaMB"] > row["UsedMB"], row
        if row["Status"] == "OverCap":
            assert row["ExcessMB"] == row["UsedMB"] - row["TargetQuotaMB"] > 0


def test_growth_margin_caps_small_sites() -> None:
    rows, summary = build_plan(SITES, {}, QuotaOptions(budget_tb=0.5, reserve_percent=0), "growth-margin")
    by_url = {r["Url"]: r for r in rows}
    assert by_url[SITES[3]["Url"]]["Status"] == "OverCap"
    assert by_url[SITES[1]["Url"]]["ProposedQuotaMB"] == 75 * GB  # 50 GB + 50 % growth
    assert summary["SitesOverCap"] == 1


def test_budget_fill_spends_the_budget_on_demand() -> None:
    # demand = usage + 50 %: 7.5, 75, 300 GB. Budget 204.8 GB -> cap 122.3 GB, the 200 GB site is OverCap.
    rows, summary = build_plan(
        SITES[:3], {}, QuotaOptions(budget_tb=0.2, reserve_percent=0, min_quota_gb=1), "budget-fill"
    )
    under = [r["ProposedQuotaMB"] for r in rows if r["Status"] == "UnderCap"]
    assert sorted(under) == [8 * GB, 75 * GB]
    assert float(summary["CapGB"]) + sum(under) / GB == pytest.approx(204.8, abs=1)


def test_fixed_quota_below_usage_is_flagged() -> None:
    rows, _ = build_plan(SITES, {SITES[3]["Url"]: 100 * GB}, QuotaOptions(budget_tb=5), "growth-margin")
    assert next(r for r in rows if r["Url"] == SITES[3]["Url"])["Status"] == "FixedBelowUsage"


def test_sessions_are_isolated_and_resolvable(tmp_path: Path) -> None:
    config = Config(None, "default", "", "", "", "browser", tmp_path)
    first = create_session(config, "Budget 3TB")
    second = create_session(config, "recycle bins")
    assert first != second
    assert resolve_session(config, "budget-3TB") == first
    assert resolve_session(config, second.name) == second
    with pytest.raises(UserError):
        resolve_session(config, "missing")


def test_helpers() -> None:
    assert _csom_date("/Date(2023,5,14,7,30,33,573)/") == "2023-06-14"
    assert _csom_date("/Date(1793765463483)/") == "2026-11-04"
    assert odata_string("it's") == "%27it%27%27s%27"
    assert server_relative("https://t.sharepoint.com/sites/a/b.csv") == "/sites/a/b.csv"


def test_export_sites_names_the_parent_team_of_channel_sites(tmp_path: Path) -> None:
    team = "/Guid(01317a73-5d4c-4963-9b40-4d8542ce66c6)/"
    zero = "/Guid(00000000-0000-0000-0000-000000000000)/"

    class FakeSharePoint:
        def list_sites(self) -> list[dict[str, object]]:
            base = {"StorageUsage": 1024, "LastContentModifiedDate": "/Date(1793765463483)/"}
            return [
                {**base, "Url": "https://t.sharepoint.com/sites/Fin", "Title": "Finance", "Template": "GROUP#0",
                 "GroupId": team, "RelatedGroupId": team},
                {**base, "Url": "https://t.sharepoint.com/sites/Fin-Cash", "Title": "Finance-Cash",
                 "Template": "TEAMCHANNEL#1", "GroupId": zero, "RelatedGroupId": team},
                {**base, "Url": "https://t.sharepoint.com/sites/Intranet", "Title": "Intranet",
                 "Template": "SITEPAGEPUBLISHING#0", "GroupId": zero, "RelatedGroupId": zero},
            ]  # fmt: skip

    rows = {r["Title"]: r["ParentTeam"] for r in read_csv(export_sites(FakeSharePoint(), tmp_path))}  # type: ignore[arg-type]
    assert rows == {"Finance": "", "Finance-Cash": "Finance", "Intranet": ""}
