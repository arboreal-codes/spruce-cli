"""spruce: SharePoint storage reduction console client."""

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from spruce_cli import cleanup, quota, versions, whatif
from spruce_cli.common import (
    Config,
    UserError,
    config_file,
    create_session,
    default_tenant,
    latest_file,
    list_profiles,
    list_sessions,
    load_config,
    log_command,
    resolve_session,
    write_config,
)
from spruce_cli.sharepoint import SharePoint
from spruce_cli.strategies import DEFAULT_STRATEGY, STRATEGIES, QuotaOptions, describe
from spruce_cli.targets import DONE_RULES, SiteFilter, select_sites

app = typer.Typer(help=__doc__, no_args_is_help=True, pretty_exceptions_enable=False)
session_app = typer.Typer(help="Create and list sessions.", no_args_is_help=True)
sites_app = typer.Typer(help="Site storage and site selection.", no_args_is_help=True)
quota_app = typer.Typer(help="Quota plans.", no_args_is_help=True)
versions_app = typer.Typer(help="Version reports, simulation, policy and trim.", no_args_is_help=True)
cleanup_app = typer.Typer(help="Recycle bins and Preservation Hold Library.", no_args_is_help=True)
app.add_typer(session_app, name="session")
app.add_typer(sites_app, name="sites")
app.add_typer(quota_app, name="quota")
app.add_typer(versions_app, name="versions")
app.add_typer(cleanup_app, name="cleanup")

# Built from the registry, so a new strategy shows up in --strategy without touching the CLI
Strategy = StrEnum("Strategy", {name: name for name in STRATEGIES})


class OverCapMode(StrEnum):
    freeze = "freeze"
    skip = "skip"


class TrimMode(StrEnum):
    count = "count"
    age = "age"
    automatic = "automatic"
    status = "status"
    cancel = "cancel"


class Stage(StrEnum):
    first = "1"
    second = "2"
    all = "all"


@dataclass
class State:
    config_path: Path | None = None
    profile: str | None = None
    session_name: str | None = None

    @property
    def config(self) -> Config:
        return load_config(self.config_path, self.profile)

    def session(self) -> Path:
        session = resolve_session(self.config, self.session_name)
        log_command(session, sys.argv[1:])
        return session

    def sharepoint(self) -> SharePoint:
        return SharePoint(self.config)


state = State()


@app.callback()
def main_options(
    config: Annotated[Path | None, typer.Option(help="Config file (default: the profile's, or ./spruce.toml).")] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", envvar="SPRUCE_PROFILE", help="Use ./spruce.<profile>.toml (see 'init')."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", envvar="SPRUCE_SESSION", help="Session name (default: the latest)."),
    ] = None,
) -> None:
    state.config_path = config
    state.profile = profile
    state.session_name = session


# ---------------------------------------------------------------- shared site selection options

SiteOpt = Annotated[
    list[str] | None, typer.Option("--site", metavar="URL", help="Site URL, repeatable. Skips the filters.")
]
PlanOpt = Annotated[Path | None, typer.Option(help="Quota plan CSV (default: latest in the session).")]
StorageOpt = Annotated[Path | None, typer.Option(help="Site-storage CSV (default: latest in the session).")]
StatusOpt = Annotated[str | None, typer.Option(help="Plan statuses, comma list: OverCap,UnderCap,Fixed.")]
MinUsedOpt = Annotated[float | None, typer.Option(help="Only sites using at least this many GB.")]
MaxUsedOpt = Annotated[float | None, typer.Option(help="Only sites using at most this many GB.")]
MatchOpt = Annotated[
    list[str] | None, typer.Option("--match", metavar="GLOB", help="Glob on URL or title, repeatable: '*Proyectos*'.")
]
TemplateOpt = Annotated[
    list[str] | None,
    typer.Option("--template", metavar="NAME", help="Site template, repeatable: GROUP, SITEPAGEPUBLISHING."),
]
InactiveOpt = Annotated[int | None, typer.Option(help="Only sites without content changes in the last N days.")]
TopOpt = Annotated[int | None, typer.Option(help="Only the N largest sites (after the other filters).")]
AllOpt = Annotated[bool, typer.Option("--all", help="Every site (when no other filter is given).")]
ExcludeOpt = Annotated[
    list[str] | None, typer.Option("--exclude", metavar="URL|GLOB", help="URL or glob to leave out, repeatable.")
]
ExcludeFileOpt = Annotated[
    list[Path] | None,
    typer.Option(
        "--exclude-file",
        metavar="PATH",
        help="CSV with Url/SiteUrl column (any spruce output) or text, one URL per line.",
    ),
]
SkipDoneOpt = Annotated[bool, typer.Option(help="Leave out sites this command already processed in any session.")]
DryRunOpt = Annotated[bool, typer.Option(help="Show what would happen, change nothing.")]
YesOpt = Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")]


def pick(
    session: Path,
    command: str,
    site: list[str] | None,
    plan: Path | None,
    storage: Path | None,
    status: str | None,
    min_used_gb: float | None,
    max_used_gb: float | None,
    match: list[str] | None,
    template: list[str] | None,
    inactive_days: int | None,
    top: int | None,
    all_sites: bool,
    exclude: list[str] | None,
    exclude_file: list[Path] | None,
    skip_done: bool,
) -> list[str]:
    site_filter = SiteFilter(
        site=site or [],
        plan=plan,
        storage=storage,
        status=status,
        min_used_gb=min_used_gb,
        max_used_gb=max_used_gb,
        match=match or [],
        template=template or [],
        inactive_days=inactive_days,
        top=top,
        all_sites=all_sites,
        exclude=exclude or [],
        exclude_file=exclude_file or [],
        skip_done=skip_done,
    )
    return select_sites(session, state.config.sessions_dir, site_filter, command)


# ---------------------------------------------------------------- login / sessions


@app.command()
def init(
    profile: Annotated[
        str | None, typer.Argument(help="Profile name: writes spruce.<profile>.toml. Omit for spruce.toml.")
    ] = None,
    admin_url: Annotated[str | None, typer.Option(help="https://<tenant>-admin.sharepoint.com")] = None,
    tenant: Annotated[str | None, typer.Option(help="Tenant domain or ID (default: from the admin URL).")] = None,
    client_id: Annotated[str | None, typer.Option(help="Entra ID app client ID.")] = None,
    auth: Annotated[str | None, typer.Option(help="browser or device-code.")] = None,
    sessions_dir: Annotated[
        str | None, typer.Option(help="Session folder (default: sessions, or sessions-<profile>).")
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing file.")] = False,
) -> None:
    """Create a config file. Missing values are asked for. Several profiles (tenants, apps) can coexist."""
    if profile is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
        raise UserError("Profile names may only use letters, digits, '-' and '_'.")
    path = config_file(profile)
    if path.exists() and not force:
        raise UserError(f"{path.name} already exists. Use --force to overwrite it.")
    admin_url = admin_url or typer.prompt("SharePoint admin URL (https://<tenant>-admin.sharepoint.com)")
    tenant = tenant or typer.prompt("Tenant", default=default_tenant(admin_url) or None)
    client_id = client_id or typer.prompt("Entra ID app client ID")
    auth = auth or typer.prompt("Login: browser or device-code", default="browser")
    sessions_dir = sessions_dir or (f"sessions-{profile}" if profile else "sessions")
    write_config(
        path,
        admin_url=admin_url,
        tenant=tenant,
        client_id=client_id,
        auth=auth,
        sessions_dir=sessions_dir,
        force=force,
    )
    print(f"Wrote {path}")
    use = f"spruce -p {profile} " if profile else "spruce "
    print(f"Next: {use}login, then {use}session new <name>")


@app.command()
def profiles() -> None:
    """List the config profiles in the current folder."""
    found = list_profiles()
    if not found:
        print("No config files here. Create one with: spruce init [PROFILE]")
    for name, path in found:
        config = load_config(path)
        print(f"{name:<15} {path.name:<28} {config.admin_url}  sessions: {config.sessions_dir}")


@app.command()
def login() -> None:
    """Log in to Microsoft 365 (delegated; the token is cached in ~/.spruce)."""
    print(f"Logged in as {state.sharepoint().login()}")


@app.command()
def logout() -> None:
    """Forget the cached login."""
    SharePoint.logout(state.config)
    print("Logged out.")


@app.command("ui")
def web_ui(
    port: Annotated[int, typer.Option(help="Local port.")] = 8765,
    browser: Annotated[bool, typer.Option(help="Open the browser.")] = True,
) -> None:
    """Open a read-only dashboard of the sessions in the browser (needs the 'ui' extra)."""
    try:
        from spruce_cli import ui
    except ImportError as error:
        raise UserError(
            "The web UI needs NiceGUI. Run it with:\n"
            '  uvx --from "spruce-cli[ui] @ git+https://github.com/arboreal-codes/spruce-cli" spruce ui'
        ) from error
    ui.run(state.config, state.session_name, port, browser)


@session_app.command("new")
def session_new(name: str, note: Annotated[str, typer.Option(help="Free text saved in session.json.")] = "") -> None:
    """Create a session folder for the reports of one run."""
    folder = create_session(state.config, name, note)
    print(f"Created session {folder.name}\n  {folder}")
    print(f"Use it with: -s {folder.name}  (or SPRUCE_SESSION={folder.name})")


@session_app.command("list")
def session_list() -> None:
    """List sessions."""
    for folder in list_sessions(state.config):
        print(f"{folder.name}  ({len(list(folder.iterdir())) - 1} files)")


@session_app.command("show")
def session_show() -> None:
    """List the files of a session."""
    session = state.session()
    print(session)
    for path in sorted(session.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(session)}")


# ---------------------------------------------------------------- sites and quotas


@sites_app.command("export")
def sites_export() -> None:
    """Step 1: export usage and quota of every site."""
    quota.export_sites(state.sharepoint(), state.session())


@sites_app.command("select")
def sites_select(
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done_for: Annotated[
        str | None, typer.Option(help=f"Apply --skip-done of one command: {', '.join(DONE_RULES)}.")
    ] = None,
) -> None:
    """Preview which sites a set of filters and exclusions selects (offline, changes nothing)."""
    if skip_done_for is not None and skip_done_for not in DONE_RULES:
        raise UserError(f"--skip-done-for must be one of: {', '.join(DONE_RULES)}")
    pick(
        state.session(), skip_done_for or "cleanup", site, plan, storage, status, min_used_gb, max_used_gb,
        match, template, inactive_days, top, all_sites, exclude, exclude_file, skip_done_for is not None,
    )  # fmt: skip


@quota_app.command("strategies")
def quota_strategies() -> None:
    """List the quota strategies."""
    for name in STRATEGIES:
        default = " (default)" if name == DEFAULT_STRATEGY else ""
        print(f"{name:<15} {describe(name)}{default}")


@quota_app.command("plan")
def quota_plan(
    budget_tb: Annotated[float, typer.Option(help="Storage budget in TB.")],
    strategy: Annotated[Strategy, typer.Option(help="See 'spruce quota strategies'.")] = Strategy[DEFAULT_STRATEGY],
    storage: StorageOpt = None,
    reserve_percent: float = 10,
    growth_margin_percent: float = 50,
    min_quota_gb: float = 10,
    freeze_margin_percent: float = 5,
    warning_percent: float = 90,
    fixed_quotas_csv: Annotated[Path | None, typer.Option(help="CSV with Url,QuotaGB.")] = None,
) -> None:
    """Step 2: compute quotas for a budget (offline)."""
    session = state.session()
    options = QuotaOptions(
        budget_tb=budget_tb,
        reserve_percent=reserve_percent,
        growth_margin_percent=growth_margin_percent,
        min_quota_gb=min_quota_gb,
        freeze_margin_percent=freeze_margin_percent,
        warning_percent=warning_percent,
    )
    storage = storage or latest_file(session, "site-storage")
    quota.plan_quotas(session, storage, options, strategy.value, fixed_quotas_csv)


@quota_app.command("apply")
def quota_apply(
    plan: PlanOpt = None,
    over_cap_mode: OverCapMode = OverCapMode.freeze,
    dry_run: DryRunOpt = False,
) -> None:
    """Step 3: apply a quota plan. Never sets a quota at or below current usage."""
    session = state.session()
    plan = plan or latest_file(session, "quota-plan")
    quota.apply_quotas(state.sharepoint(), session, plan, over_cap_mode.value, dry_run)


# ---------------------------------------------------------------- versions


@versions_app.command("report-start")
def report_start(
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done: SkipDoneOpt = False,
    library: str = "VersionReports",
    force: Annotated[bool, typer.Option(help="Request even if a job is still running in some session.")] = False,
    dry_run: DryRunOpt = False,
) -> None:
    """Step 4: queue version storage reports. Skips sites with a report still running in any session."""
    session = state.session()
    sites = pick(
        session, "report", site, plan, storage, status, min_used_gb, max_used_gb, match, template,
        inactive_days, top, all_sites, exclude, exclude_file, skip_done,
    )  # fmt: skip
    versions.start_reports(state.sharepoint(), session, state.config.sessions_dir, sites, library, dry_run, force)


@versions_app.command("report-receive")
def report_receive() -> None:
    """Step 4: check the session's report jobs and download finished reports."""
    versions.receive_reports(state.sharepoint(), state.session())


@versions_app.command("whatif")
def versions_whatif(
    report_path: Annotated[Path | None, typer.Option(help="Report CSV or folder (default: session's reports).")] = None,
    major_version_limit: Annotated[str, typer.Option(help="Comma list, 0 = no limit.")] = "100",
    expire_after_days: Annotated[str, typer.Option(help="Comma list, 0 = no limit.")] = "0,365",
    as_of: Annotated[datetime | None, typer.Option(help="Date to simulate from (default: now).")] = None,
    plan: PlanOpt = None,
    include_file_detail: bool = False,
) -> None:
    """Step 5: simulate version policies over the downloaded reports (offline)."""
    session = state.session()
    if plan is None and list(session.glob("quota-plan_*.csv")):
        plan = latest_file(session, "quota-plan")
    whatif.run(
        report_path or session / versions.REPORTS_DIR,
        session,
        session / versions.JOBS_FILE,
        counts=whatif.int_list(major_version_limit),
        days_list=whatif.int_list(expire_after_days),
        as_of=as_of,
        plan=plan,
        include_file_detail=include_file_detail,
    )


@versions_app.command("policy")
def versions_policy(
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done: SkipDoneOpt = False,
    major_version_limit: int = 100,
    expire_after_days: Annotated[int, typer.Option(help="0 = never.")] = 0,
    major_with_minor_limit: int = 0,
    automatic: bool = False,
    apply_existing: Annotated[bool, typer.Option(help="Also existing libraries.")] = False,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
) -> None:
    """Step 6: set version history limits (deletes nothing)."""
    session = state.session()
    sites = pick(
        session, "policy", site, plan, storage, status, min_used_gb, max_used_gb, match, template,
        inactive_days, top, all_sites, exclude, exclude_file, skip_done,
    )  # fmt: skip
    versions.set_policy(
        state.sharepoint(),
        session,
        sites,
        major_limit=major_version_limit,
        expire_days=expire_after_days,
        minor_limit=major_with_minor_limit,
        automatic=automatic,
        apply_existing=apply_existing,
        dry_run=dry_run,
        assume_yes=yes,
    )


@versions_app.command("trim")
def versions_trim(
    mode: Annotated[TrimMode, typer.Option(help="count, age, automatic, status or cancel.")],
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done: SkipDoneOpt = False,
    major_version_limit: int = 0,
    major_with_minor_limit: int = 0,
    delete_before_days: int = 0,
    allow_below_policy_minimum: bool = False,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
) -> None:
    """Step 6: queue, check or cancel version trim jobs. Trims are PERMANENT."""
    session = state.session()
    sites = pick(
        session, "trim", site, plan, storage, status, min_used_gb, max_used_gb, match, template,
        inactive_days, top, all_sites, exclude, exclude_file, skip_done,
    )  # fmt: skip
    sp = state.sharepoint()
    if mode == TrimMode.status:
        versions.trim_status(sp, sites)
    elif mode == TrimMode.cancel:
        versions.trim_cancel(sp, sites)
    else:
        versions.start_trim(
            sp,
            session,
            sites,
            mode=mode.value,
            major_limit=major_version_limit,
            minor_limit=major_with_minor_limit,
            delete_days=delete_before_days,
            allow_below=allow_below_policy_minimum,
            dry_run=dry_run,
            assume_yes=yes,
        )


# ---------------------------------------------------------------- cleanup


@cleanup_app.command("analyze")
def cleanup_analyze(
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done: SkipDoneOpt = False,
    detail: Annotated[bool, typer.Option(help="Also one row per recycle bin item / held file.")] = False,
) -> None:
    """Space held in recycle bins and the Preservation Hold Library."""
    session = state.session()
    sites = pick(
        session, "cleanup", site, plan, storage, status, min_used_gb, max_used_gb, match, template,
        inactive_days, top, all_sites, exclude, exclude_file, skip_done,
    )  # fmt: skip
    cleanup.analyze(state.sharepoint(), session, sites, detail)


@cleanup_app.command("purge-recycle-bin")
def cleanup_purge(
    site: SiteOpt = None,
    plan: PlanOpt = None,
    storage: StorageOpt = None,
    status: StatusOpt = None,
    min_used_gb: MinUsedOpt = None,
    max_used_gb: MaxUsedOpt = None,
    match: MatchOpt = None,
    template: TemplateOpt = None,
    inactive_days: InactiveOpt = None,
    top: TopOpt = None,
    all_sites: AllOpt = False,
    exclude: ExcludeOpt = None,
    exclude_file: ExcludeFileOpt = None,
    skip_done: SkipDoneOpt = False,
    stage: Stage = Stage.second,
    older_than_days: Annotated[int, typer.Option(help="0 = every item in the stage.")] = 0,
    dry_run: DryRunOpt = False,
    yes: YesOpt = False,
) -> None:
    """PERMANENTLY delete recycle bin items."""
    session = state.session()
    sites = pick(
        session, "purge", site, plan, storage, status, min_used_gb, max_used_gb, match, template,
        inactive_days, top, all_sites, exclude, exclude_file, skip_done,
    )  # fmt: skip
    cleanup.purge_recycle_bin(
        state.sharepoint(),
        session,
        sites,
        stage=stage.value,
        older_than_days=older_than_days,
        dry_run=dry_run,
        assume_yes=yes,
    )


def main() -> None:
    try:
        app()
    except UserError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
