"""Local read-only dashboard (NiceGUI). Started by `spruce ui`; reads session files only."""

from collections import defaultdict
from pathlib import Path
from typing import Any

from nicegui import ui

from spruce_cli import dashboard
from spruce_cli.common import Config, UserError, list_sessions, resolve_session
from spruce_cli.strategies import STRATEGIES, QuotaOptions
from spruce_cli.targets import SiteFilter

# Validated categorical order (blue, orange, aqua, yellow) plus reserved status colors.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NEUTRAL = "#a8a6a0"
STATUS_COLORS = {"OverCap": "#d03b3b", "UnderCap": "#2a78d6"}  # anything else (Fixed...) is neutral
TOP_SITES = 25


def _label(row: dict[str, Any]) -> str:
    return row.get("Title") or str(row.get("Url") or row.get("SiteUrl") or "").rstrip("/").rsplit("/", 1)[-1]


def _table(rows: list[dict[str, Any]], row_key: str = "Url") -> None:
    if not rows:
        return
    columns: list[dict[str, Any]] = [
        {"name": k, "label": k, "field": k, "sortable": True, "align": "left"} for k in rows[0]
    ]
    key = row_key if row_key in rows[0] else columns[0]["name"]
    ui.table(rows=rows, columns=columns, row_key=key, pagination=20).classes("w-full").props("dense flat bordered")


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


def _sites_tab(session: Path) -> None:
    f = SiteFilter()

    with ui.row().classes("items-end gap-4 flex-wrap"):
        status = ui.select(["OverCap", "UnderCap"], label="Status", multiple=True, clearable=True).classes("w-48")
        min_gb = ui.number("Min used GB", min=0).classes("w-32")
        max_gb = ui.number("Max used GB", min=0).classes("w-32")
        match = ui.input("Match (URL or title)").classes("w-48")
        template = ui.input("Template (e.g. GROUP)").classes("w-40")
        inactive = ui.number("Inactive days", min=0, precision=0).classes("w-32")
        top = ui.number("Top", min=1, precision=0).classes("w-24")

    @ui.refreshable
    def results() -> None:
        rows = dashboard.sites(session, f)
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
        _table(rows)

    def apply() -> None:
        f.status = ",".join(status.value or []) or None
        f.min_used_gb = min_gb.value
        f.max_used_gb = max_gb.value
        f.match = [match.value] if match.value else []
        f.template = [template.value] if template.value else []
        f.inactive_days = int(inactive.value) if inactive.value is not None else None
        f.top = int(top.value) if top.value is not None else None
        results.refresh()

    for control in (status, min_gb, max_gb, match, template, inactive, top):
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

    @ui.refreshable
    def results() -> None:
        options = QuotaOptions(
            budget_tb=budget.value or defaults.budget_tb,
            reserve_percent=reserve.value or 0,
            growth_margin_percent=growth.value or 0,
            freeze_margin_percent=freeze.value or 0,
            min_quota_gb=min_quota.value or defaults.min_quota_gb,
        )
        try:
            summaries, plans = dashboard.compare_strategies(session, options)
        except (UserError, ValueError) as e:
            _empty(str(e))
            return
        ui.label("All strategies with these options (in memory, nothing is saved)").classes("text-sm text-gray-600")
        _table(summaries, row_key="Strategy")

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

    ui.label("Saved plans in this session").classes("text-lg font-medium mt-6")
    if saved:
        _table(saved, row_key="File")
    else:
        _empty("None yet. Save one with 'spruce quota plan'.")


def _whatif_tab(session: Path) -> None:
    rows, path = dashboard.whatif(session)
    if not rows:
        _empty("No simulation yet. Run 'spruce versions whatif' after receiving version reports.")
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
        _table([r for r in rows if site.value in (all_sites, r["SiteUrl"])], row_key="Scenario")

    site.on_value_change(chart.refresh)
    chart()


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
    _table(rows, row_key="SiteUrl")


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
    sessions = list_sessions(config)
    if not sessions:
        raise UserError("No sessions yet. Create one with: spruce session new <name>")
    start = resolve_session(config, session_name)

    @ui.page("/")
    def index() -> None:
        ui.colors(primary=SERIES[0])
        ui.query("body").style("background-color: #fcfcfb; color: #0b0b0b")
        current = {"session": start}

        with ui.header().classes("items-center gap-4 bg-white text-black border-b"):
            ui.label("spruce").classes("text-xl font-semibold")
            ui.label(f"profile: {config.profile}").classes("text-gray-600")
            ui.space()
            names = [p.name for p in list_sessions(config)]
            picker = ui.select(names, value=start.name, label="Session").classes("w-96")
            ui.button(icon="refresh", on_click=lambda: body.refresh()).props("flat round").tooltip("Reload files")

        @ui.refreshable
        def body() -> None:
            session = current["session"]
            tabs_spec = [
                ("Sites", _sites_tab),
                ("Quota plans", _quota_tab),
                ("Version simulation", _whatif_tab),
                ("Recycle bin & holds", _cleanup_tab),
                ("Session", _session_tab),
            ]
            with ui.tabs().classes("w-full") as tabs:
                tab_items = [ui.tab(name) for name, _ in tabs_spec]
            with ui.tab_panels(tabs, value=tab_items[0]).classes("w-full bg-transparent"):
                for item, (_, render) in zip(tab_items, tabs_spec, strict=True):
                    with ui.tab_panel(item).classes("gap-4"):
                        render(session)

        def switch(e: Any) -> None:
            current["session"] = config.sessions_dir / e.value
            body.refresh()

        picker.on_value_change(switch)
        body()

    print(f"spruce UI on http://127.0.0.1:{port} (Ctrl+C to stop)")
    ui.run(host="127.0.0.1", port=port, title="spruce", show=show, reload=False, show_welcome_message=False)
