# LLM Usage Reports

Periodic cost/usage reports for LLM coding agents (Claude Code, Codex, Gemini CLI)
across every configured dev machine. First report generated 2026-06-10 covering
2026-05-11 through 2026-06-09.

**This directory is a public repo:** https://github.com/digitaldrywood/llm-usage-reports

Keep every identifying detail — hostnames, usernames, SSH targets, deploy
endpoints, real spend figures — in `config.json` (gitignored) or `.envrc`
(gitignored), never in code, tests, or docs. `config.sample.json` is the committed
template; update it whenever you add a config key. Test fixtures use
`machine-a`/`machine-b`, never real hostnames. `README.md` is written for outside
readers; this file is the working notes and is committed alongside it.

Note this file is force-added (`git add -f`) because `CLAUDE.md` is in the user's
global gitignore — `git add -A` will not pick up changes to it. Commit it
explicitly.

**If this working copy sits inside a file-sync folder** (Dropbox, iCloud Drive,
OneDrive, Google Drive), exclude `.git/` from the sync. A syncer racing git on
`index`, `refs/`, and loose objects is a well-known way to corrupt a repo, and
it's worse when the same folder is replicated to a second machine that also runs
git. Tracked files keep syncing normally; only `.git/` is excluded.

For Dropbox on Linux, set the extended attribute (note `setfattr`/`attr` may not
be installed — Python works without them):

```bash
python3 -c "import os; os.setxattr('.git', 'user.com-dropbox-ignored', b'1')"
python3 -c "import os; print(os.getxattr('.git', 'user.com-dropbox-ignored'))"
```

It is not stored in git, so re-apply it after moving or re-cloning in place.

`scripts/refresh.sh` runs **hourly** via cron (`0 * * * *`). A full run takes ~12s
and makes no LLM calls (it only reads local agent logs), so the cadence is
effectively free. The script self-locks with `flock -n -E 99` so a wedged SSH to a
remote machine can't let runs pile up; a lock conflict logs a skip line and exits 0.

Publishing is **not** the repo's job — `generate_report.py` writes HTML into
`reports/` and stops. If `config.json` sets `publishCommand`, it runs from the
project root after a successful render and a non-zero exit fails the run. Locally
that points at `scripts/publish.local.sh` (gitignored via `*.local.sh`; it reads
its deploy credentials from `.envrc`). Never name the destination service in a
committed file.

## Folder layout

```
LLM Usage/
  README.md            - public-facing overview
  CLAUDE.md            - this file (working notes)
  config.sample.json   - committed config template
  config.json          - real config: machines, timezone, publish hook (gitignored)
  raw/                 - raw ccusage JSON per machine  (gitignored)
                         rolling: ccusage-<machine-id>-rolling.json
                         frozen:  ccusage-<machine-id>-<since>_<until>.json
  reports/             - generated HTML reports        (gitignored)
                         naming: llm-usage-summary-<run-date>.html
```

## Machines

Usage data lives locally on each machine (ccusage reads the agents' local logs),
so EVERY machine must be collected — none can see another's data. The machine
list comes from `config.json`; there is nothing machine-specific in the code.

- Exactly one entry omits `ssh` — the machine the script runs on.
- Every other entry is driven over SSH and needs working key-based auth.
- `remoteShell` (default `zsh -lc`) wraps the remote command so a login shell's
  PATH applies — required when `npx` comes from Homebrew or a version manager.
- `id` determines the raw filename, so renaming one starts a fresh snapshot.

If a remote machine is on Tailscale and MagicDNS doesn't resolve (`tailscale
status` health warning), put the raw `100.x` IP in `ssh` rather than the hostname.

## Collection command

```bash
python3 scripts/generate_report.py --collect --days 30
```

The generator collects unified `--breakdown --by-agent` JSON on each machine,
then runs a second source-specific `ccusage codex daily --speed standard` pass
and reconciles the GPT model costs day by day. That second pass is required
because ccusage 20.0.17's unified `daily` command does not honor
`codex.defaults.speed`; on a machine currently configured for fast mode it can
retroactively apply fast multipliers to old sessions. The checked-in offline
config also supplies Sonnet 5's introductory price and Opus 5's rate, neither of
which is reliably in ccusage 20.0.17's embedded snapshot.

`--by-agent` is load-bearing, not cosmetic. Since 2026-07-19 the **`claude`
agent also reports GPT rows** — Claude Code delegating to Codex through the
codex plugin writes GPT usage into Claude Code's own logs. `ccusage codex daily`
only ever sees the Codex CLI source, so reconciling the *merged* GPT total
against it compares two different populations: it reads as a Fast-mode
detection, divides every GPT row, and then hard-fails the per-day check. Only
the `codex` agent's rows are reconciled; the delegated rows pass through at
their logged cost and are reported as `_llmUsageReport.delegatedGptCost`. They
still count as Codex in the agent split (classification is by model prefix), so
the headline Codex number covers both sources.

If the unified GPT cost is higher than the explicit Standard pass, the
generator records a durable incident in
`.ccusage/codex-fast-incidents.json`, normalizes the report to Standard, and
renders a prominent Fast-mode warning. Codex logs do not contain an
authoritative per-turn Fast flag, so the incident is evidence that Fast was
configured/detected at collection—not proof that every historical turn used
Fast. To prevent accidental selection, keep `features.fast_mode = false` in
both machines' Codex `config.toml`; use `/fast off` in any already-running
session.

## Building the report

1. Collect both JSONs into `raw/` (commands above).
2. Sanity check: `jq '[.daily[].totalCost] | add' <file>` on each; the sum of
   both must equal the combined total you report. Reconcile to the cent.
3. Merge per-day per-model across machines (sum costs by date + modelName).
   Days can be missing from one machine (no usage) — treat as 0, and build the
   full date range so day labels align across series.
4. Generate the HTML into `reports/` — see the existing report as the template:
   dark GitHub-style theme, Chart.js from jsdelivr CDN, stat cards on top, then
   dual-axis daily chart (stacked spend bars on the left axis, a token-volume
   line on the right `y1` axis), stacked daily bar by model, model split
   doughnut + by-model table, agent split doughnut + cumulative line, weekly
   totals, by-machine table, highlights table. The token line uses muted grey
   and `drawOnChartArea: false` on `y1` so it reads as a reference overlay and
   its gridlines don't double up on the spend grid.
   There is deliberately **no** status banner above the cards — Fast-mode
   incidents are still recorded in `.ccusage/codex-fast-incidents.json` and
   printed to stdout (so the cron log is the alerting channel), but they no
   longer push the data below the fold.
5. Model grouping convention: GPT-5.5, GPT-5.6 Sol (etc. per major GPT
   model, all counted as Codex in the agent split), Opus 5, Opus 4.8, Opus 4.7,
   Fable 5, Sonnet 5, Haiku 4.5, Sonnet 4.6, Gemini; fold trace GPT usage (e.g.
   gpt-5.4-mini) into "Other GPT (Codex)", older Opus (4.5/4.6) into "Other
   Claude", and trace Claude/Gemini usage into the matching provider family.
   When a new model ships, add it to MODEL_GROUPS + group_of() in
   scripts/generate_report.py. Provider totals are classified from the raw model
   prefix, so Gemini and unknown models never silently count as Claude.
   `group_of()` matches each headline model on its exact alias plus dated
   snapshots (`claude-opus-5-20260701`) — never a bare prefix, so a future
   `claude-opus-5-1` gets its own group instead of being charted as Opus 5.
   `load()` prints a WARNING for any model that falls through to "Other", or
   that reports tokens at $0 cost while belonging to a hosted provider we price
   (local Ollama models are legitimately free and are exempt). Either warning
   means the report is quietly undercounting and the run needs a code or
   pricing-override change.
6. ccusage detects more sources than the four in `AGENTS` — `openclaw` and
   `opencode` both appear in the deeper history. Some of them report models
   with a bracketed agent prefix (`[openclaw] gpt-5.4`), which `normalize_model()`
   strips before classification; without that, real GPT spend was misfiled to
   Other/Other. Keep classifying by normalized model name, not by ccusage's
   agent field, so the provider split stays stable as new CLIs appear.

## Gotchas

- ccusage prints a separate report per detected data source; a local run only
  ever covers that one machine. Don't present one machine's numbers as the
  total.
- Costs are API-equivalent ccusage estimates from token counts — not invoices
  or subscription-limit percentages. Fable 5's rate card is already 2x Opus
  4.8; never multiply its calculated cost by 2 again. ChatGPT fast-mode credit
  multipliers are a separate plan-usage metric and are intentionally excluded
  by `codex.defaults.speed: standard`. Footnote this distinction.
- A Fast-mode incident banner is a separate ChatGPT-credit warning. Preserve
  the incident history across rolling snapshot refreshes so a resolved incident
  remains visible until its observed report window rolls out.
- Cache reads dominate tokens (~97%); call that out rather than implying raw
  generation volume.
- Pin the ccusage version (`ccusage@20.0.17` as of 2026-07) and use the offline
  pricing config so both machines compute identical, repeatable pricing. Online
  mode fetches live pricing and can retroactively revalue the same token logs.
- Sonnet 5's override is the introductory $2/$10 per MTok rate through
  2026-08-31. Review the config before the 2026-09-01 standard-price change.
- Opus 5's override is $5/$25 per MTok — the same rate card as Opus 4.8, so a
  4.8 → 5 migration should not move the cost line by itself. Cache-write is
  1.25x input and cache-read 0.1x input, matching the Sonnet 5 override's shape.
  Opus 5 **fast mode** bills at $10/$50; ccusage prices from token logs and does
  not distinguish it, so a fast-mode-heavy period is undercounted here. Treat
  that the same way as the ChatGPT fast-mode credit caveat.
- **The rolling window ends on today, not yesterday** — an hourly refresh is
  only worth publishing if today's spend is in it. Today is partial, so it is
  excluded from Avg/Day, the peak-day figure, and the biggest/quietest-day
  rankings (it gets its own "Today so far" highlight row instead). It is also
  exempt from the strict Codex reconciliation: the unified and Codex passes are
  separate ccusage invocations seconds apart, so an agent logging in between
  makes today's two readings legitimately differ. Settled days still reconcile
  to the cent, and today settles on the first run after midnight. Pass
  `live_date` to `normalize_codex_standard()` whenever the window includes a day
  that is still being written.
- **Backfill is possible as long as the source logs survive.** Reports are
  regenerated from each machine's local agent logs on every run, never
  accumulated, so a failed cron run only leaves the *published* page stale —
  re-running recovers the window. But history depth differs sharply by source
  and machine — when last measured, Codex history reached back roughly 3x further
  than Claude Code history on the same box. A 30-day rolling window fits
  comfortably, but a longer
  retrospective would silently undercount Claude while Codex looked complete.
  `cleanupPeriodDays` is unset (default 30) on both machines, so raising it is
  one lever. The durable answer is the monthly archive: with
  `archiveMonthly: true` (default) the first run of each month freezes the
  previous month to `raw/ccusage-<id>-<first>_<last>.json`, written once and
  never overwritten. Backfill older months while their logs still exist:
  `python3 scripts/generate_report.py --archive-month 2026-06` (add `--force`
  to redo one). Archives are collected with no `live_date`, so they reconcile
  strictly — the month must be over, and the command refuses an unfinished one.
  Note the two original hand-made windows (`…05-11_2026-06-09`,
  `…06-10_2026-06-15`) overlap the calendar-month archives, so never sum every
  dated file and call it a lifetime total.
- Re-collecting the same window can move the total slightly (~0.01% observed)
  because ccusage re-reads live session files that agents may still rewrite.
  Don't treat a past rolling total as immutable; the dated milestone reports in
  `reports/` are the fixed record.
- `gemini-3.5-flash` is missing from ccusage's offline snapshot (it reported
  tokens at $0 until 2026-07-28). The override is Google's published paid-tier
  rate: $1.50 in / $9.00 out / $0.15 cached-read per MTok. Gemini also bills
  context-cache **storage** at $1.00 per MTok per hour — that's time-based and
  ccusage prices from token counts alone, so cached-heavy Gemini use is slightly
  undercounted here. Cache-creation is set to the input rate (Gemini has no
  write premium), unlike the Claude overrides where it is 1.25x input.
