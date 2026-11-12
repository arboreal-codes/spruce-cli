"""Local dashboard (NiceGUI), started by `spruce ui`.

Views work on session files only. Actions that call SharePoint (export, version reports,
cleanup analysis) run the same functions as the CLI, one at a time, with their output streamed
to a log dialog. Nothing here changes quotas, policies or deletes content.
"""

import asyncio
import contextlib
import io
import queue
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from nicegui import run as nicegui_run
from nicegui import ui

from spruce_cli import cleanup, dashboard, quota, versions, whatif
from spruce_cli.common import Config, UserError, create_session, list_sessions, log_command, resolve_session
from spruce_cli.sharepoint import SharePoint
from spruce_cli.strategies import STRATEGIES, QuotaOptions
from spruce_cli.targets import DONE_RULES, SiteFilter

# Validated categorical order (blue, orange, aqua, yellow) plus reserved status colors.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NEUTRAL = "#a8a6a0"
STATUS_COLORS = {"OverCap": "#d03b3b", "UnderCap": "#2a78d6"}  # anything else (Fixed...) is neutral
TOP_SITES = 25


# Runs a SharePoint action: (title, session, CLI argv for history.log, function of the SharePoint client).
Act = Callable[[str, Path, list[str], Callable[[SharePoint], object]], Awaitable[None]]


class _LineWriter(io.TextIOBase):
    """File-like object that hands complete lines to a queue (print() output from a worker thread)."""

    def __init__(self, sink: queue.SimpleQueue[str]) -> None:
        self.sink, self.buffer = sink, ""

    def write(self, text: str) -> int:
        self.buffer += text
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            self.sink.put(line)
        return len(text)

    def flush(self) -> None:
        if self.buffer:
            self.sink.put(self.buffer)
            self.buffer = ""


def _capture(fn: Callable[[], object], sink: queue.SimpleQueue[str]) -> None:
    """Run fn with its printed output sent to sink; errors become the last line."""
    # ponytail: redirect_stdout is process-wide, so only one action runs at a time (see the lock in run()).
    writer = _LineWriter(sink)
    with contextlib.redirect_stdout(writer):
        try:
            fn()
            print("Done.")
        except Exception as error:  # noqa: BLE001 - shown to the user, the server keeps running
            print(f"ERROR: {error}")
        finally:
            writer.flush()


def _label(row: dict[str, Any]) -> str:
    return row.get("Title") or str(row.get("Url") or row.get("SiteUrl") or "").rstrip("/").rsplit("/", 1)[-1]


def _download(rows: list[dict[str, Any]], filename: str, label: str = "CSV") -> None:
    ui.button(label, icon="download", on_click=lambda: ui.download.content(dashboard.to_csv(rows), filename)).props(
        "flat dense no-caps"
    )


def _table(
    rows: list[dict[str, Any]], row_key: str = "Url", filename: str = "spruce.csv", selection: bool = False
) -> ui.table | None:
    """Sortable, paginated table with a CSV download of all its rows."""
    if not rows:
        return None
    columns: list[dict[str, Any]] = [
        {"name": k, "label": k, "field": k, "sortable": True, "align": "left"} for k in rows[0]
    ]
    key = row_key if row_key in rows[0] else columns[0]["name"]
    with ui.row().classes("w-full justify-end -mb-2"):
        _download(rows, filename)
    table = ui.table(
        rows=rows, columns=columns, row_key=key, pagination=20, selection="multiple" if selection else None
    )
    table.classes("w-full").props("dense flat bordered")
    return table


def _bar_chart(categories: list[str], series: list[dict[str, Any]], unit: str = "GB", height: int = 0) -> None:
    """Horizontal bar chart: one category per row, legend for 2+ series, tooltip on hover."""
    for s in series:
        s.setdefault("type", "bar")
        s.setdefault("barMaxWidth", 18)
        s.setdefault("itemStyle", {}).setdefault("borderRadius", [0, 4, 4, 0])
    options = {
        "grid": {"left": 8, "right": 40, "top": 36 if len(series) > 1 else 8, "bottom": 8, "containLabel": True},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}, ":valueFormatter": f"(v) => v + ' {unit}'"},
        "legend": {"show": len(series) > 1, "top": 0, "textStyle": {"color": "#52514e"}},
        "xAxis": {"type": "value", "name": unit, "splitLine": {"lineStyle": {"color": "#e8e7e4"}}},
        "yAxis": {
            "type": "category",
            "inverse": True,
            "data": categories,
            "axisLabel": {"width": 220, "overflow": "truncate", "color": "#52514e"},
            "axisTick": {"show": False},
        },
        "series": series,
    }  # keys prefixed with ':' are evaluated as JavaScript by NiceGUI
    ui.echart(options).classes("w-full").style(f"height: {height or 60 + 26 * len(categories)}px")


def _empty(text: str) -> None:
    ui.label(text).classes("text-gray-500 italic")


def _sites_tab(session: Path, act: Act, picked: list[str]) -> None:
    f = SiteFilter()

    ui.button(
        "Export sites from SharePoint",
        icon="cloud_download",
        on_click=lambda: act("Export sites", session, ["sites", "export"], lambda sp: quota.export_sites(sp, session)),
    ).props("outline no-caps")

    with ui.row().classes("items-end gap-4 flex-wrap"):
        status = ui.select(["OverCap", "UnderCap"], label="Status", multiple=True, clearable=True).classes("w-48")
        min_gb = ui.number("Min used GB", min=0).classes("w-32")
        max_gb = ui.number("Max used GB", min=0).classes("w-32")
        match = ui.input("Match (URL or title)").classes("w-48")
        template = ui.input("Template (e.g. GROUP)").classes("w-40")
        inactive = ui.number("Inactive days", min=0, precision=0).classes("w-32")
        top = ui.number("Top", min=1, precision=0).classes("w-24")
        exclude = ui.input("Exclude (URL pattern, * wildcard)").classes("w-64")
        skip_done = ui.select(
            {"": "Don't skip", **{c: f"Done by '{c}'" for c in DONE_RULES}}, value="", label="Skip sites already"
        ).classes("w-48")

    @ui.refreshable
    def results() -> None:
        rows, skipped = dashboard.sites(session, f, skip_done.value or "cleanup")
        total = sum(r["UsedGB"] for r in rows)
        with ui.row().classes("items-center gap-6"):
            ui.label(f"{len(rows)} sites · {total:,.1f} GB used").classes("text-lg font-medium")
            ui.label(f"spruce ... {dashboard.filter_flags(f)}").classes("font-mono text-sm text-gray-600")
        if not rows:
            _empty("No sites. Run 'spruce sites export' in this session, or relax the filters.")
            return
        shown = rows[:TOP_SITES]
        statuses = list(dict.fromkeys(r["Status"] or "No plan" for r in shown))
        series = [
            {
                "name": s,
                "stack": "used",
                "color": STATUS_COLORS.get(s, NEUTRAL),
                "data": [r["UsedGB"] if (r["Status"] or "No plan") == s else "-" for r in shown],
            }
            for s in statuses
        ]
        ui.label(f"Largest {len(shown)} sites by used storage").classes("text-sm text-gray-600")
        _bar_chart([_label(r) for r in shown], series)
        if skipped:
            with ui.expansion(f"{len(skipped)} sites left out by exclusions").classes("w-full"):
                _table(skipped, filename="sites-skipped.csv")
        ui.label("Select sites to use them in a command or as an exclude list").classes("text-sm text-gray-600")
        with ui.row().classes("gap-2"):
            copy = ui.button("Copy as --site", icon="content_copy").props("outline dense no-caps")
            save = ui.button("Download selected (for --exclude-file)", icon="download").props("outline dense no-caps")
            reports = ui.button("Request version reports", icon="history").props("outline dense no-caps")
            analyze = ui.button("Analyze recycle bins & holds", icon="delete_sweep").props("outline dense no-caps")
        flags = ui.label().classes("font-mono text-sm select-all break-all")  # fallback if the clipboard is blocked
        table = _table(rows, filename="sites.csv", selection=True)
        assert table is not None
        table.on_select(lambda: picked.__setitem__(slice(None), [r["Url"] for r in table.selected]))

        def selected() -> list[dict[str, Any]]:
            if not table.selected:
                ui.notify("Select some sites first", type="warning")
            return table.selected

        def copy_flags() -> None:
            if urls := [r["Url"] for r in selected()]:
                flags.text = " ".join(f"--site {u}" for u in urls)
                ui.clipboard.write(flags.text)
                ui.notify(f"{len(urls)} --site options copied (also shown below the buttons)")

        def save_selected() -> None:
            if rows_ := selected():
                ui.download.content(dashboard.to_csv(rows_), "sites-selected.csv")

        def site_flags(urls: list[str]) -> list[str]:
            return [arg for u in urls for arg in ("--site", u)]

        async def request_reports() -> None:
            if not (urls := [r["Url"] for r in selected()]):
                return
            with ui.dialog() as confirm, ui.card().classes("w-[32rem]"):
                ui.label(f"Request version reports for {len(urls)} sites?").classes("text-lg font-medium")
                ui.label(
                    "Creates a 'VersionReports' library in each site if missing and queues a report job "
                    "(1 or more days). Sites with a job still running in any session are skipped."
                ).classes("text-sm text-gray-600")
                with ui.row().classes("w-full justify-end"):
                    ui.button("Cancel", on_click=lambda: confirm.submit(None)).props("flat no-caps")
                    ui.button("Dry run", on_click=lambda: confirm.submit(True)).props("outline no-caps")
                    ui.button("Request", on_click=lambda: confirm.submit(False)).props("no-caps")
            dry_run = await confirm
            if dry_run is None:
                return
            argv = ["versions", "report-start", *site_flags(urls), *(["--dry-run"] if dry_run else [])]
            await act(
                "Request version reports" + (" (dry run)" if dry_run else ""),
                session,
                argv,
                lambda sp: versions.start_reports(sp, session, session.parent, urls, "VersionReports", dry_run, False),
            )

        async def analyze_cleanup() -> None:
            if urls := [r["Url"] for r in selected()]:
                await act(
                    "Analyze recycle bins & holds",
                    session,
                    ["cleanup", "analyze", *site_flags(urls)],
                    lambda sp: cleanup.analyze(sp, session, urls, False),
                )

        copy.on_click(copy_flags)
        save.on_click(save_selected)
        reports.on_click(request_reports)
        analyze.on_click(analyze_cleanup)

    def apply() -> None:
        f.status = ",".join(status.value or []) or None
        f.min_used_gb = min_gb.value
        f.max_used_gb = max_gb.value
        f.match = [match.value] if match.value else []
        f.template = [template.value] if template.value else []
        f.inactive_days = int(inactive.value) if inactive.value is not None else None
        f.top = int(top.value) if top.value is not None else None
        f.exclude = [exclude.value] if exclude.value else []
        f.skip_done = bool(skip_done.value)
        results.refresh()

    for control in (status, min_gb, max_gb, match, template, inactive, top, exclude, skip_done):
        control.on_value_change(apply)
    results()


def _quota_tab(session: Path) -> None:
    saved = dashboard.saved_plans(session)
    budget_default = float(saved[0]["BudgetGB"]) / 1024 if saved and saved[0].get("BudgetGB") else 1.0
    defaults = QuotaOptions(budget_tb=budget_default)

    with ui.row().classes("items-end gap-4 flex-wrap"):
        budget = ui.number("Budget TB", value=defaults.budget_tb, min=0.1, step=0.1).classes("w-28")
        reserve = ui.number("Reserve %", value=defaults.reserve_percent, min=0, max=90).classes("w-28")
        growth = ui.number("Growth margin %", value=defaults.growth_margin_percent, min=0).classes("w-32")
        freeze = ui.number("Freeze margin %", value=defaults.freeze_margin_percent, min=0).classes("w-32")
        min_quota = ui.number("Min quota GB", value=defaults.min_quota_gb, min=1).classes("w-28")

        strategy = ui.select(list(STRATEGIES), value=next(iter(STRATEGIES)), label="Strategy to save").classes("w-44")
        save = ui.button("Save plan", icon="save").props("no-caps")

    def options() -> QuotaOptions:
        return QuotaOptions(
            budget_tb=budget.value or defaults.budget_tb,
            reserve_percent=reserve.value or 0,
            growth_margin_percent=growth.value or 0,
            freeze_margin_percent=freeze.value or 0,
            min_quota_gb=min_quota.value or defaults.min_quota_gb,
        )

    @ui.refreshable
    def results() -> None:
        try:
            summaries, plans = dashboard.compare_strategies(session, options())
        except (UserError, ValueError) as e:
            _empty(str(e))
            return
        ui.label("All strategies with these options (in memory, nothing is saved)").classes("text-sm text-gray-600")
        _table(summaries, row_key="Strategy", filename="quota-compare.csv")

        first = next(iter(plans.values()))
        top_rows = sorted(first.values(), key=lambda r: -float(r["UsedMB"]))[:15]
        series = [{"name": "Used", "color": NEUTRAL, "data": [round(float(r["UsedMB"]) / 1024, 1) for r in top_rows]}]
        for color, name in zip(SERIES, STRATEGIES, strict=False):
            plan = plans[name]
            series.append(
                {
                    "name": name,
                    "color": color,
                    "data": [round(float(plan[r["Url"]]["ProposedQuotaMB"]) / 1024, 1) for r in top_rows],
                }
            )
        ui.label("Proposed quota per strategy, 15 largest sites").classes("text-sm text-gray-600 mt-4")
        _bar_chart([_label(r) for r in top_rows], series, height=60 + 15 * 18 * len(series))

    for control in (budget, reserve, growth, freeze, min_quota):
        control.on_value_change(results.refresh)
    results()

    @ui.refreshable
    def saved_plans() -> None:
        ui.label("Saved plans in this session").classes("text-lg font-medium mt-6")
        if rows := dashboard.saved_plans(session):
            _table(rows, row_key="File", filename="quota-plans.csv")
        else:
            _empty("None yet. Save one above or with 'spruce quota plan'.")

    def save_plan() -> None:
        try:
            path = dashboard.save_plan(session, options(), strategy.value)
        except (UserError, ValueError) as e:
            ui.notify(str(e), type="negative")
            return
        ui.notify(f"Saved {path.name}", type="positive")
        saved_plans.refresh()

    save.on_click(save_plan)
    saved_plans()


def _whatif_tab(session: Path, act: Act) -> None:
    reports = session / versions.REPORTS_DIR
    with ui.row().classes("items-end gap-4 flex-wrap"):
        limits = ui.input("Major version limits", value="100").classes("w-40")
        limits.tooltip("Comma list, 0 = no limit")
        days = ui.input("Expire after days", value="0,365").classes("w-40")
        days.tooltip("Comma list, 0 = no limit, else at least 30")

        def simulate() -> Awaitable[None]:
            plans = sorted(session.glob("quota-plan_*.csv"))  # like the CLI: latest plan, if any
            return act(
                "Simulate version policies",
                session,
                ["versions", "whatif", "--major-version-limit", limits.value, "--expire-after-days", days.value],
                lambda _sp: whatif.run(
                    reports,
                    session,
                    session / versions.JOBS_FILE,
                    counts=whatif.int_list(limits.value),
                    days_list=whatif.int_list(days.value),
                    as_of=None,
                    plan=plans[-1] if plans else None,
                    include_file_detail=False,
                ),
            )

        run_button = ui.button("Run simulation", icon="play_arrow", on_click=simulate).props("outline no-caps")
        if not any(reports.glob("*.csv")):
            run_button.disable()
            run_button.tooltip("Receive version reports first (Version reports tab)")
    ui.label("Offline: reads the downloaded version reports, changes nothing in SharePoint.").classes(
        "text-sm text-gray-600"
    )
    rows, path = dashboard.whatif(session)
    if not rows:
        _empty("No simulation yet. Run one above after receiving version reports.")
        return
    ui.label(f"From {path.name if path else ''}").classes("text-sm text-gray-600")
    all_sites = "All sites"
    site = ui.select([all_sites, *dict.fromkeys(r["SiteUrl"] for r in rows)], value=all_sites, label="Site")
    site.classes("w-[36rem]")

    @ui.refreshable
    def chart() -> None:
        totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for r in rows:
            if site.value in (all_sites, r["SiteUrl"]):
                totals[r["Scenario"]][0] += r["SustainableFreedGB"]
                totals[r["Scenario"]][1] += r["TemporaryFreedGB"]
        scenarios = list(totals)
        series = [
            {
                "name": "Sustainable",
                "stack": "freed",
                "color": SERIES[0],
                "data": [round(totals[s][0], 1) for s in scenarios],
            },
            {
                "name": "Temporary",
                "stack": "freed",
                "color": SERIES[1],
                "data": [round(totals[s][1], 1) for s in scenarios],
            },
        ]
        ui.label("Space freed per scenario (sustainable stays freed, temporary grows back)").classes(
            "text-sm text-gray-600"
        )
        _bar_chart(scenarios, series)
        _table([r for r in rows if site.value in (all_sites, r["SiteUrl"])], row_key="Scenario", filename="whatif.csv")

    site.on_value_change(chart.refresh)
    chart()


def _reports_tab(session: Path, act: Act) -> None:
    if (session / versions.JOBS_FILE).exists():
        ui.button(
            "Check jobs and download finished reports",
            icon="cloud_download",
            on_click=lambda: act(
                "Receive version reports",
                session,
                ["versions", "report-receive"],
                lambda sp: versions.receive_reports(sp, session),
            ),
        ).props("outline no-caps")
    jobs = dashboard.report_jobs(session.parent)
    if not jobs:
        _empty("No version reports requested yet. Run 'spruce versions report-start'.")
        return
    only_this = ui.switch("Only this session", value=True)

    @ui.refreshable
    def view() -> None:
        rows = [j for j in jobs if not only_this.value or j["Session"] == session.name]
        counts: dict[str, int] = defaultdict(int)
        for j in rows:
            counts[j.get("Status", "")] += 1
        with ui.row().classes("gap-2"):
            for status, color in (("Requested", "grey"), ("InProgress", "primary"), ("Downloaded", "positive")):
                ui.chip(f"{status}: {counts.pop(status, 0)}", color=color, text_color="white").props("dense")
            for status, n in counts.items():
                ui.chip(f"{status or 'Unknown'}: {n}", color="negative", text_color="white").props("dense")
        if not rows:
            _empty("No report jobs in this session.")
            return
        ui.label("Reports take 1 or more days; check again later to refresh pending jobs.").classes(
            "text-sm text-gray-600"
        )
        _table(rows, row_key="SiteUrl", filename="version-report-jobs.csv")

    only_this.on_value_change(view.refresh)
    view()


def _cleanup_tab(session: Path) -> None:
    rows, path = dashboard.cleanup(session)
    if not rows:
        _empty("No cleanup analysis yet. Run 'spruce cleanup analyze'.")
        return
    ui.label(f"From {path.name if path else ''}").classes("text-sm text-gray-600")
    shown = rows[:TOP_SITES]
    parts = [
        ("Recycle bin (1st stage)", "RecycleStage1GB"),
        ("Recycle bin (2nd stage)", "RecycleStage2GB"),
        ("Preservation Hold", "PreservationHoldGB"),
    ]
    series = [
        {"name": name, "stack": "held", "color": color, "data": [r[key] for r in shown]}
        for color, (name, key) in zip(SERIES, parts, strict=False)
    ]
    _bar_chart([_label(r) for r in shown], series)
    _table(rows, row_key="SiteUrl", filename="cleanup-summary.csv")


def _command_tab(session: Path, profile: str | None, picked: list[str]) -> None:
    """Build a CLI command from its options, to run in a terminal (the dashboard never runs these)."""
    commands = dashboard.cli_commands()
    ui.label(
        "Build any spruce command with its options and copy it to run in a terminal. "
        "The options come from the CLI itself; the session and profile are filled in."
    ).classes("text-sm text-gray-600")
    choice = ui.select(list(commands), value="quota apply", label="Command", with_input=True).classes("w-72")

    @ui.refreshable
    def form() -> None:
        path = choice.value
        help_text, params = commands[path]
        ui.label(help_text).classes("font-medium")
        if path in dashboard.CHANGES_TENANT:
            ui.label("Changes the tenant: run it with --dry-run first, then without it.").classes(
                "text-sm text-white bg-[#d03b3b] rounded px-2 py-1"
            )
        fields: dict[str, Any] = {}
        output = ui.code("", language="bash").classes("w-full")
        missing = ui.label().classes("text-sm text-[#d03b3b]")

        def update() -> None:
            values = {name: element.value for name, element in fields.items()}
            output.set_content(dashboard.build_command(path, params, values, session.name, profile))
            needed = [p.flag or p.name for p in params if p.required and values.get(p.name) in (None, "")]
            missing.text = f"Still required: {', '.join(needed)}" if needed else ""

        with ui.grid(columns="repeat(auto-fill, minmax(18rem, 1fr))").classes("w-full gap-x-6 gap-y-2"):
            for p in params:
                label = (p.flag or p.name) + (" *" if p.required else "")
                if p.kind == "flag":
                    on = bool(p.default) or (p.name == "dry_run" and path in dashboard.CHANGES_TENANT)
                    element: Any = ui.checkbox(label, value=on)
                elif p.kind == "choice":
                    element = ui.select(p.choices, value=p.default, label=label, clearable=not p.required)
                elif p.kind == "multi":
                    element = ui.textarea(label, placeholder="one per line").props("autogrow")
                else:
                    placeholder = "" if p.default in (None, "", 0, False) else str(p.default)
                    element = ui.input(label, placeholder=placeholder)
                if p.help:
                    element.tooltip(p.help)
                element.on_value_change(update)
                fields[p.name] = element

        if "site" in fields:

            def use_selection() -> None:
                if not picked:
                    ui.notify("Select sites in the Sites tab first", type="warning")
                    return
                fields["site"].value = "\n".join(picked)

            ui.button("Use the sites selected in the Sites tab", icon="checklist", on_click=use_selection).props(
                "outline dense no-caps"
            )
        ui.label("Hover an option for its help; 'spruce <command> --help' shows them all.").classes(
            "text-xs text-gray-500"
        )
        update()

    choice.on_value_change(form.refresh)
    form()


def _session_tab(session: Path) -> None:
    info = dashboard.session_info(session)
    meta = info["meta"]
    ui.label(meta.get("name", session.name)).classes("text-xl font-medium")
    ui.label(f"{session.name} · created {meta.get('created_at', '')} · {meta.get('note', '')}").classes("text-gray-600")
    ui.label(str(session)).classes("font-mono text-sm text-gray-500")
    with ui.row().classes("w-full gap-8 items-start"):
        with ui.column().classes("min-w-64 gap-1"):
            ui.label("Files").classes("font-medium")
            for name in info["files"]:
                ui.label(name).classes("font-mono text-sm")
        with ui.column().classes("flex-1"):
            ui.label("History").classes("font-medium")
            ui.code("\n".join(info["history"]) or "(empty)", language="text").classes("w-full")


def run(config: Config, session_name: str | None, port: int, show: bool) -> None:
    # No sessions yet: start empty and let the user create one from the header.
    start = resolve_session(config, session_name) if list_sessions(config) else None

    busy = asyncio.Lock()  # one SharePoint action at a time, for every browser tab

    @ui.page("/")
    def index() -> None:
        ui.colors(primary=SERIES[0])
        ui.query("body").style("background-color: #fcfcfb; color: #0b0b0b")
        current: dict[str, Any] = {"session": start, "tab": "Sites"}  # the tab survives refreshes after actions
        picked: list[str] = []  # sites selected in the Sites tab, for the command builder
        profile = config.profile if config.profile != "default" else None

        with ui.header().classes("items-center gap-4 bg-white text-black border-b"):
            ui.label("🌲 spruce").classes("text-xl font-semibold")
            ui.label(f"profile: {config.profile}").classes("text-gray-600")
            ui.space()

            @ui.refreshable
            def account() -> None:
                if user := dashboard.signed_in_account(config):
                    ui.label(user).classes("text-sm text-gray-600")
                else:
                    ui.button(
                        "Log in", icon="login", on_click=lambda: act("Log in", None, [], lambda sp: sp.login())
                    ).props("flat no-caps")

            account()
            names = [p.name for p in list_sessions(config)]
            picker = ui.select(names, value=start.name if start else None, label="Session").classes("w-96")
            ui.button(icon="add", on_click=lambda: dialog.open()).props("flat round").tooltip("New session")
            ui.button(icon="refresh", on_click=lambda: body.refresh()).props("flat round").tooltip("Reload files")

        with ui.dialog() as dialog, ui.card().classes("w-96"):
            ui.label("New session").classes("text-lg font-medium")
            new_name = ui.input("Name", validation={"Required": bool}).classes("w-full")
            new_note = ui.input("Note (optional)").classes("w-full")

            def create() -> None:
                if not new_name.value:
                    return
                folder = create_session(config, new_name.value, new_note.value or "")
                picker.set_options([p.name for p in list_sessions(config)], value=folder.name)
                new_name.value = new_note.value = ""
                dialog.close()
                ui.notify(f"Created {folder.name}", type="positive")

            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                ui.button("Create", on_click=create).props("no-caps")

        lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        with ui.dialog().props("persistent") as log_dialog, ui.card().classes("w-[48rem] max-w-full"):
            log_title = ui.label().classes("text-lg font-medium")
            log = ui.log(max_lines=2000).classes("w-full h-96 font-mono text-xs")
            close = ui.button("Close", on_click=log_dialog.close).props("no-caps")
        ui.timer(0.2, lambda: [log.push(lines.get()) for _ in range(lines.qsize())])

        async def act(title: str, session: Path | None, argv: list[str], fn: Callable[[SharePoint], object]) -> None:
            if busy.locked():
                ui.notify("Another action is still running", type="warning")
                return
            async with busy:
                log.clear()
                log_title.text = title
                close.disable()
                log_dialog.open()
                if session is not None and argv:
                    log_command(session, [*argv, " # from spruce ui"])
                await nicegui_run.io_bound(_capture, lambda: fn(SharePoint(config)), lines)
                await asyncio.sleep(0.3)  # let the timer flush the last lines
                close.enable()
            account.refresh()
            body.refresh()

        @ui.refreshable
        def body() -> None:
            session = current["session"]
            if session is None:
                with ui.column().classes("w-full items-center gap-4 mt-16"):
                    ui.label("No sessions yet").classes("text-xl font-medium")
                    ui.label("A session is a folder that keeps the reports of one run.").classes("text-gray-600")
                    ui.button("Create session", icon="add", on_click=dialog.open).props("no-caps")
                return
            tabs_spec = [
                ("Sites", lambda s: _sites_tab(s, act, picked)),
                ("Quota plans", _quota_tab),
                ("Version reports", lambda s: _reports_tab(s, act)),
                ("Version simulation", lambda s: _whatif_tab(s, act)),
                ("Recycle bin & holds", _cleanup_tab),
                ("Command builder", lambda s: _command_tab(s, profile, picked)),
                ("Session", _session_tab),
            ]
            with ui.tabs(value=current["tab"]).classes("w-full") as tabs:
                tab_items = [ui.tab(name) for name, _ in tabs_spec]
            tabs.on_value_change(lambda e: current.__setitem__("tab", e.value))
            with ui.tab_panels(tabs, value=current["tab"]).classes("w-full bg-transparent"):
                for item, (_, render) in zip(tab_items, tabs_spec, strict=True):
                    with ui.tab_panel(item).classes("gap-4"):
                        render(session)

        def switch(e: Any) -> None:
            current["session"] = config.sessions_dir / e.value
            body.refresh()

        picker.on_value_change(switch)
        body()

    print(f"spruce UI on http://127.0.0.1:{port} (Ctrl+C to stop)")
    ui.run(
        host="127.0.0.1", port=port, title="spruce", favicon="🌲", show=show, reload=False, show_welcome_message=False
    )
