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
    rows, skipped = dashboard.sites(session, SiteFilter(min_used_gb=100))
    assert skipped == []
    assert [(r["Title"], r["UsedGB"], r["QuotaGB"]) for r in rows] == [("Big", 200.0, 300.0)]
    assert dashboard.filter_flags(SiteFilter(min_used_gb=100, top=5)) == "--min-used-gb 100 --top 5"
    assert dashboard.filter_flags(SiteFilter()) == "--all"
    assert dashboard.sites(tmp_path, SiteFilter()) == ([], [])  # no exports: empty, not an error


def test_sites_left_out_by_exclusions(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    old = tmp_path / "20261101-000000_old"
    old.mkdir()
    (old / "cleanup-summary_20261101-000000.csv").write_text(f"SiteUrl,Message\n{BASE}/small,\n")
    rows, skipped = dashboard.sites(session, SiteFilter(exclude=["*/big"], skip_done=True), "cleanup")
    assert rows == []
    assert skipped == [
        {"Url": f"{BASE}/big", "Reason": "excluded"},
        {"Url": f"{BASE}/small", "Reason": "done in 20261101-000000_old"},
    ]
    assert dashboard.filter_flags(SiteFilter(exclude=["*/big"], skip_done=True)) == "--exclude '*/big' --skip-done"


def test_report_jobs_and_csv(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    (session / "version-report-jobs.csv").write_text(
        f"SiteUrl,RequestedAt,Status\n{BASE}/big,2026-11-01T00:00:00,Requested\n"
    )
    jobs = dashboard.report_jobs(tmp_path)
    assert jobs == [
        {"Session": session.name, "SiteUrl": f"{BASE}/big", "RequestedAt": "2026-11-01T00:00:00", "Status": "Requested"}
    ]
    assert dashboard.to_csv(jobs).splitlines()[0] == "Session,SiteUrl,RequestedAt,Status"


def test_save_plan_writes_session_files(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    path = dashboard.save_plan(session, QuotaOptions(budget_tb=0.5), "water-filling")
    assert path.exists() and path.name.endswith("_water-filling.csv")
    assert dashboard.saved_plans(session)[0]["Strategy"] == "water-filling"
    assert "quota plan --budget-tb 0.5" in (session / "history.log").read_text()


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


def test_command_builder_reads_cli_and_quotes() -> None:
    commands = dashboard.cli_commands()
    assert "ui" not in commands and "versions trim" in commands
    _, params = commands["versions trim"]
    kinds = {p.name: p.kind for p in params}
    assert kinds["mode"] == "choice" and kinds["site"] == "multi" and kinds["dry_run"] == "flag"
    values = {"mode": "count", "site": f"{BASE}/a\n\n{BASE}/b", "major_version_limit": "100", "dry_run": True}
    assert dashboard.build_command("versions trim", params, values, "s1", "acme") == (
        f"spruce -p acme -s s1 versions trim --mode count --site {BASE}/a --site {BASE}/b "
        "--major-version-limit 100 --dry-run"
    )
    _, params = commands["session new"]
    assert dashboard.build_command("session new", params, {"name": "my run"}) == "spruce session new 'my run'"
