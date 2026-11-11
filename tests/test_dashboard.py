from pathlib import Path

from spruce_cli import dashboard
from spruce_cli.strategies import STRATEGIES, QuotaOptions
from spruce_cli.targets import SiteFilter

BASE = "https://t.sharepoint.com/sites"


def make_session(tmp_path: Path) -> Path:
    session = tmp_path / "20261110-000000_now"
    session.mkdir()
    (session / "session.json").write_text('{"name": "now"}')
    (session / "site-storage_20261110-000000.csv").write_text(
        "Url,Title,Template,LastContentModifiedDate,UsedMB,QuotaMB\n"
        f"{BASE}/big,Big,GROUP#0,2026-01-01,{200 * 1024},{300 * 1024}\n"
        f"{BASE}/small,Small,GROUP#0,2026-01-01,{5 * 1024},{300 * 1024}\n"
    )
    return session


def test_sites_and_flags(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    rows = dashboard.sites(session, SiteFilter(min_used_gb=100))
    assert [(r["Title"], r["UsedGB"], r["QuotaGB"]) for r in rows] == [("Big", 200.0, 300.0)]
    assert dashboard.filter_flags(SiteFilter(min_used_gb=100, top=5)) == "--min-used-gb 100 --top 5"
    assert dashboard.filter_flags(SiteFilter()) == "--all"
    assert dashboard.sites(tmp_path, SiteFilter()) == []  # no exports: empty, not an error


def test_compare_strategies_plans_every_strategy(tmp_path: Path) -> None:
    summaries, plans = dashboard.compare_strategies(make_session(tmp_path), QuotaOptions(budget_tb=0.1))
    assert [s["Strategy"] for s in summaries] == list(STRATEGIES)
    assert all(set(p) == {f"{BASE}/big", f"{BASE}/small"} for p in plans.values())


def test_whatif_converts_to_gb(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    (session / "version-whatif-sites_20261110-000002.csv").write_text(
        f"SiteUrl,Scenario,FreedMB,SustainableFreedMB,TemporaryFreedMB\n{BASE}/big,count10_age0d,2048,1024,1024\n"
    )
    rows, path = dashboard.whatif(session)
    assert path is not None
    assert rows[0]["FreedGB"] == 2.0 and rows[0]["SustainableFreedGB"] == 1.0 and "FreedMB" not in rows[0]
