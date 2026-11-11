# spruce

**English** | [Español](README.es.md)

Cut down the SharePoint Online storage a Microsoft 365 tenant uses, step by step and without
locking anyone out.

spruce (project `spruce-cli`, command `spruce`) is a cross-platform console tool (macOS, Linux, Windows) that measures every site,
computes per-site quotas for a storage budget, simulates version history policies against
SharePoint's own version reports, applies policies and version trims, and finds space held in
recycle bins and the Preservation Hold Library. Every run's reports are kept in a session
folder, so rounds never mix.

It talks to SharePoint directly (MSAL login + REST/CSOM). No PowerShell, PnP, Node or `m365` CLI
needed; only [uv](https://docs.astral.sh/uv/).

## How it works, in short

Storage is brought down to a budget B in rounds. Each round:

1. **Measure** every site.
2. **Plan quotas** for B with a _water-filling_ cap: small sites keep room to grow, large ones
   are **frozen** just above their current usage (never below it, which would make them
   read-only) and get a target of what they must free.
3. **Apply** the quotas.
4. Ask SharePoint for **version reports** of the frozen sites (one row per file version).
5. **Simulate** version policies (keep N versions, drop versions older than D days) to see what
   each would free per site and per file.
6. Set the **version policy** (so the problem does not come back), **trim** existing versions
   (permanent, with confirmations and safeguards), and purge **recycle bins**.
7. Wait ~48 h, measure again, and lower B when the round is done.

The full method, its rules and its trade-offs are in **[docs/strategy.md](docs/strategy.md)**.
Read it before changing anything in a production tenant.

## Quick start

Install uv:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Run spruce straight from GitHub (uv downloads Python and the dependencies the first time):

```bash
uvx --from git+https://github.com/arboreal-codes/spruce-cli spruce --help
```

Or install it as a regular command, and upgrade it later:

```bash
uv tool install git+https://github.com/arboreal-codes/spruce-cli
uv tool upgrade spruce-cli
```

Add `@v0.2.1` (a tag) or `@<commit>` to the URL to pin a version.

Then, in the folder where you want to keep config and reports:

```bash
spruce init                          # asks for tenant values, writes spruce.toml
spruce login                         # browser or device code
spruce session new "budget 5TB"
spruce sites export                  # step 1
spruce quota plan --budget-tb 5      # step 2, offline
spruce quota apply --dry-run         # step 3, preview
```

## Prerequisites

1. **An Entra ID app registration of your own** (Microsoft provides no shared one):
   - Platform _Mobile and desktop applications_ with redirect URI `http://localhost`.
   - _Allow public client flows_ = Yes (needed for `--auth device-code`).
   - API permission **SharePoint → Delegated → AllSites.FullControl**, with admin consent.

   An app created with `m365 setup` (CLI for Microsoft 365) or
   `Register-PnPEntraIDAppForInteractiveLogin` (PnP.PowerShell) already meets this.

2. **An account with the SharePoint Administrator role.** For version reports and trims it must
   also be **site collection admin** of each site. These jobs only work with a delegated (user)
   login, not app-only.
3. **Manual site storage limits** in the SharePoint admin center (Settings → Site storage
   limits). See [one-time tenant setup](docs/strategy.md#one-time-tenant-setup).

## Configuration and profiles

`spruce init` asks for the admin center URL, the tenant (suggested from the URL), the app's
client ID and the login mode, and writes `spruce.toml` in the current folder. Pass
`--admin-url`, `--tenant`, `--client-id`, `--auth`, `--sessions-dir` to skip the questions;
`--force` overwrites. [`config.example.toml`](config.example.toml) documents every key.

Give `init` a name to keep several configurations side by side (other tenant, other app, test
vs production). Each profile has its own file, session folder and login cache:

```bash
spruce init contoso                  # -> spruce.contoso.toml, sessions in sessions-contoso/
spruce init lab --auth device-code
spruce profiles                      # list the config files in this folder
spruce -p contoso login
export SPRUCE_PROFILE=contoso        # Windows: $env:SPRUCE_PROFILE="contoso"
```

Config lookup: `--config PATH`, else the profile (`-p` / `SPRUCE_PROFILE`), else
`SPRUCE_CONFIG`, else `./spruce.toml`. The login token is cached in `~/.spruce/`, one cache
per tenant + app.

## Sessions

Every report goes into a session folder, so rounds never mix and two can run at once (for
example a quota round in one terminal and a recycle bin cleanup in another).

```bash
spruce session new "budget 3TB" --note "november round"   # -> sessions/20261110-231344_budget-3TB/
spruce session list
spruce -s budget-3TB session show                          # files of a session
```

Pick a session with `-s/--session` (folder name, name or timestamp prefix) or `SPRUCE_SESSION`.
Both are global options and go before the command (`spruce -s budget-3TB quota plan ...`).
Without either, the latest session is used and a notice is printed. Inside a session, commands
default to its latest input file (the latest `site-storage` for the plan, the latest
`quota-plan` for everything after it). Each session keeps `session.json` and a `history.log` of
the commands run in it.

## Commands

| Step               | Command                                                                             |
| ------------------ | ----------------------------------------------------------------------------------- |
| 1. Measure         | `spruce sites export`                                                              |
| 2. Quota plan      | `spruce quota plan --budget-tb 3 [--strategy NAME]`                                |
| 3. Apply quotas    | `spruce quota apply --dry-run`, then without `--dry-run`                           |
| 4. Version reports | `spruce versions report-start --status OverCap`, then `report-receive` daily       |
| 5. Simulate        | `spruce versions whatif --major-version-limit 10,50,100 --expire-after-days 0,365` |
| 6. Policy          | `spruce versions policy --site URL --major-version-limit 100 --apply-existing`     |
| 6. Trim            | `spruce versions trim --site URL --mode count --major-version-limit 100`           |
| 6. Recycle bins    | `spruce cleanup analyze --status OverCap` / `cleanup purge-recycle-bin`            |
| Any                | `spruce sites select ...` previews a site selection                                |
| Any                | `spruce ui` opens the [web dashboard](#web-dashboard)                              |

`spruce --help` and `spruce <command> --help` list every option. What each step does and why
is in [docs/strategy.md](docs/strategy.md).

**Safeguards:** quotas at or below current usage are never applied; policies below 100 versions
are rejected; trims below 100 need `--allow-below-policy-minimum`; trims are read back and
cancelled if SharePoint registered something else; version reports are never requested twice
for a site while one runs; destructive commands support `--dry-run` and ask for a typed
confirmation (skip with `--yes`).

## Choosing sites: filters and exclusions

Every command that acts on sites (`versions report-start`, `policy`, `trim`, `cleanup analyze`,
`cleanup purge-recycle-bin`) takes the same options. Candidates are the session's latest
`site-storage` export joined with its latest `quota-plan` (override with `--storage` / `--plan`).
Filters combine with AND:

| Option                                   | Keeps                                                            |
| ---------------------------------------- | ---------------------------------------------------------------- |
| `--status OverCap,Fixed`                 | sites with those plan statuses                                   |
| `--min-used-gb 80` / `--max-used-gb 500` | sites by used storage                                            |
| `--match '*Projects*'`                   | URL or title matches the glob (repeatable, OR)                   |
| `--template GROUP`                       | site template (`GROUP`, `STS`, `SITEPAGEPUBLISHING`; repeatable) |
| `--inactive-days 365`                    | no content changes in the last N days (archive candidates)       |
| `--top 10`                               | only the N largest, after the other filters                      |
| `--all`                                  | every site, when you really mean it                              |

A command with no filter refuses to run, so nothing hits every site by accident. `--site URL`
(repeatable) picks sites explicitly and skips the filters. Exclusions apply after the filters,
also to `--site`:

| Option                  | Leaves out                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `--exclude URL_OR_GLOB` | matching sites (repeatable)                                                                                                           |
| `--exclude-file PATH`   | sites listed in a CSV with a `Url` or `SiteUrl` column (any spruce output works) or a text file with one URL per line (`#` comments) |
| `--skip-done`           | sites this same command already processed successfully in **any** session                                                             |

"Done" means: `cleanup analyze` = row without error in a `cleanup-summary`; `report-start` =
report `Downloaded`; `policy` = `Applied`; `trim` = `Queued`; `purge-recycle-bin` = `Deleted`.

```bash
spruce sites select --min-used-gb 80 --skip-done-for cleanup        # preview
spruce cleanup analyze --min-used-gb 80 --skip-done --exclude-file never-touch.txt
spruce versions report-start --status OverCap --top 5 --exclude '*Archive*'
```

`report-start` also skips any site with a report still `Requested` or `InProgress` in any
session (a new request would supersede it and lose the days already waited). If that job did
finish, run `report-receive` in its session; `--force` requests anyway.

## Quota strategies

```bash
spruce quota strategies
spruce quota plan --budget-tb 3 --reserve-percent 15 --strategy budget-fill
```

| Strategy                  | UnderCap quota                                                                    |
| ------------------------- | --------------------------------------------------------------------------------- |
| `growth-margin` (default) | min(c, used × (1 + growth margin))                                                |
| `water-filling`           | the full cap c                                                                    |
| `budget-fill`             | min(demand, c′), with demand = used × (1 + growth) and Σ min(demand, c′) = budget |

All of them freeze OverCap sites at used × (1 + freeze margin) and honor fixed quotas
(`--fixed-quotas-csv` with `Url,QuotaGB`). The plan file name includes the strategy, so several
can be compared in one session. Trade-offs: [docs/strategy.md](docs/strategy.md#quota-strategies).

**Adding a strategy:** write a function in [`src/spruce_cli/strategies.py`](src/spruce_cli/strategies.py)
that takes the used MB of each site, the budget in MB and `QuotaOptions`, and returns
`(cap, [Allocation, ...])`. Register it in `STRATEGIES`; the first docstring line is its
description. The CLI, CSV output and summary pick it up.

## Recycle bins and Preservation Hold Library

Both count toward a site's storage but do not show up in version reports.

```bash
spruce cleanup analyze --min-used-gb 20 --detail
spruce cleanup purge-recycle-bin --site URL --stage 2 --older-than-days 30 --dry-run
```

- `analyze` writes `cleanup-summary_<ts>.csv`: per site, used MB, items and MB in each recycle
  bin stage (and how much was deleted more than 30 days ago), Preservation Hold Library files
  and MB, and the share of used storage held there. `--detail` adds one row per item / held file.
- `purge-recycle-bin` **permanently** deletes recycle bin items (stage `1`, `2` or `all`,
  optionally only those deleted more than N days ago).
- The Preservation Hold Library is only reported: it is freed by changing the retention policy in
  Microsoft Purview. Only the root web's library is measured (subsites have their own).

## Web dashboard

A local dashboard over the session files: sites with the same filters and exclusions as the
CLI, all quota strategies compared side by side with the options you type, version report jobs
of every session, the version simulation and the recycle bin / Preservation Hold analysis.

Select sites in the table to copy them as `--site` options, download them as a CSV for
`--exclude-file`, request their version reports or analyze their recycle bins; every table has a
CSV download.

The views only read session files. Actions that call SharePoint (log in, export sites, request
and receive version reports, cleanup analysis) run the same code as the CLI, one at a time, with
their output in a log window, and are recorded in the session's `history.log`. The dashboard
also writes new sessions and quota plans ("Save plan"). It never changes quotas or version
policies and never deletes anything: use the CLI for that.

The "Command builder" tab helps with those: pick any command, fill its options (read from the
CLI itself, with each option's help on hover) and copy the resulting command, with the session
and profile already set. Sites selected in the Sites tab can fill `--site`, and commands that
change the tenant start with `--dry-run` checked. The command is quoted for macOS/Linux shells;
on Windows PowerShell, check the quotes before running it.

```bash
uvx --from "spruce-cli[ui] @ git+https://github.com/arboreal-codes/spruce-cli" spruce ui
```

It listens on `http://127.0.0.1:8765` only (`--port` to change it, `--no-browser` to not open
it). Pick the session in the header; `-s` and `-p` work as in every command.

## Development

```bash
git clone https://github.com/arboreal-codes/spruce-cli && cd spruce-cli
uv run spruce --help
uv run pytest
uv run ruff check --fix && uv run ruff format
uv run ty check
```

| Module (`src/spruce_cli/`) | Purpose                                    |
| ----------------------- | ------------------------------------------ |
| `cli.py`                | Typer commands                             |
| `common.py`             | config, profiles, sessions, CSV helpers    |
| `sharepoint.py`         | MSAL login, REST and CSOM calls            |
| `targets.py`            | site filters and exclusions                |
| `strategies.py`         | quota strategies                           |
| `quota.py`              | steps 1 to 3                               |
| `versions.py`           | steps 4 and 6                              |
| `whatif.py`             | step 5 (Polars)                            |
| `cleanup.py`            | recycle bins and Preservation Hold Library |
| `dashboard.py`          | data for the web dashboard (no UI imports) |
| `ui.py`                 | web dashboard (NiceGUI, optional `ui` extra) |

Calls that change SharePoint are not covered by the tests (they need a tenant). Try new code with
`--dry-run` and on a small site first.

## Disclaimer

Version trims and recycle bin purges delete data permanently. spruce is provided as is, without
warranty (see [LICENSE](LICENSE)). Run the simulation and `--dry-run` first, and get the site
owners' approval.

## License

[MIT](LICENSE)

## Why "spruce"

The name puts together three ideas:

- **SP**, for SharePoint.
- **A tree.** spruce was built at Arboreal, a forestry company in Uruguay: a sawmill that
  produces mass timber and builds projects with it. A tree name felt right. Arboreal does not
  work with spruce, though; it works mostly with pine and some eucalyptus. Spruce was chosen
  for its letters and its verb, not for the species.
- **To spruce up**: to tidy something until it looks neat, which is what the tool does to a
  tenant's storage. Pruning old versions so the rest can keep growing is also a forester's job.

The project and repository are `spruce-cli` because `spruce` was already taken on PyPI; the
command stays `spruce`.
