# LLM Coding Agent Usage Reports

A small pipeline that answers one question: **what am I actually spending on AI
coding agents?**

It reads the local logs that Claude Code, Codex, Gemini CLI and friends already
write, across every machine you work on, and renders a single dark-themed HTML
dashboard — daily spend by agent with token volume on a second axis, spend by
model, provider split, cumulative burn, per-machine totals.

No API keys, no telemetry, no account linking. Everything is computed from logs
already on disk by [ccusage](https://github.com/ryoppippi/ccusage). This project
is the multi-machine collection, normalization, and reporting layer on top.

<!-- Screenshot: reports/llm-usage-latest.html rendered in a browser. -->

## Why it isn't just `ccusage daily`

Running ccusage by hand gets you a table for *one* machine with *today's*
pricing. The parts that took real work:

- **Multi-machine.** ccusage only sees the logs on the box it runs on. This
  collects each machine (locally or over SSH), reconciles them, and sums to the
  cent — it refuses to publish a partial total if one machine is unreachable.
- **Repeatable pricing.** Pinned ccusage version + a checked-in offline pricing
  snapshot, so the same token logs always value the same. Online mode fetches
  live prices and will silently revalue your history.
- **Codex fast-mode normalization.** ccusage's unified `daily` command has
  ignored `codex.defaults.speed`, so a machine currently set to fast mode can
  retroactively multiply old sessions. A second source-specific pass at
  `--speed standard` reconciles the GPT costs day by day, and any discrepancy is
  recorded as a durable incident rather than quietly smoothed over.
- **Two sources of GPT spend.** Claude Code delegating to Codex logs GPT usage
  into *Claude Code's* logs, which `ccusage codex daily` never sees. Reconciling
  the merged total against it produces a false fast-mode detection and then a
  hard failure. Only the Codex CLI's own rows are reconciled.
- **Partial-day handling.** The window ends on *today* so an hourly refresh has
  something new to show, but today is still being written — so it's excluded
  from averages, day rankings, and the strict reconciliation, and labelled as
  partial in the UI.
- **Fails loudly.** Reconciliation mismatches abort the run. Unknown models and
  hosted models priced at $0 print warnings instead of silently undercounting.

## Setup

Requires Python 3.10+, Node (for `npx`), and key-based SSH to any remote machine
you want included.

```bash
git clone <this repo>
cd llm-usage-reports
cp config.sample.json config.json
$EDITOR config.json          # machines, timezone, optional publish hook
./scripts/refresh.sh
```

Output lands in `reports/` — open `reports/index.html`.

### config.json

`config.json` is gitignored; `config.sample.json` documents every field.

```json
{
  "timezone": "America/Chicago",
  "machines": [
    { "id": "workstation", "label": "Workstation (Linux)" },
    { "id": "laptop", "label": "Laptop (macOS)",
      "ssh": "you@host", "remoteShell": "zsh -lc" }
  ],
  "publishCommand": null
}
```

Exactly one machine omits `ssh` — that's the one you run the script on. Every
other machine is driven over SSH; `remoteShell` wraps the remote command so a
login shell's PATH applies (needed when `npx` comes from Homebrew or a version
manager). `id` determines the raw snapshot filename, so changing it starts a
fresh one.

### Publishing

**This project only writes HTML into `reports/`.** Where that goes is your
business. Set `publishCommand` to your own script — rsync, S3, Netlify, a Pages
branch, whatever — and it runs from the project root after a successful render;
a non-zero exit fails the run. Keep that script out of the repo: `*.local.sh` is
gitignored. Leave `publishCommand` as `null` and nothing is published.

### Scheduling

The run takes ~12s and makes no LLM calls, so it's cheap to run often. Hourly:

```cron
0 * * * * cd /path/to/llm-usage-reports && ./scripts/refresh.sh >> logs/refresh.log 2>&1
```

`refresh.sh` self-locks with `flock`, so a wedged SSH can't let runs pile up.

## Adding a new model

Models are grouped for charting in `MODEL_GROUPS` + `group_of()` in
`scripts/generate_report.py`. When a new one ships, add it there — until you do,
it charts as "Other" and the run prints a warning telling you so. If ccusage's
offline snapshot doesn't know its price yet, add a `pricingOverrides` entry to
`.ccusage/ccusage.json` (the run warns about that too, for hosted models
reporting tokens at $0 — local Ollama models are exempt, since free is correct).

## Layout

```
scripts/generate_report.py   collection, normalization, reconciliation, rendering
scripts/refresh.sh           the scheduled entry point (tests → collect → render)
scripts/test_generate_report.py
.ccusage/ccusage.json        pinned offline pricing + overrides
config.sample.json           config template (copy to config.json)
raw/                         collected ccusage JSON, per machine   (gitignored)
reports/                     generated HTML                        (gitignored)
```

The reconciliation logic in `generate_report.py` is heavily commented — each
guard says which real failure put it there, because most of them are not
obvious and removing one tends to reintroduce a silent undercount.

## Caveats

Costs are **API-equivalent estimates** computed from token counts — not
invoices, not subscription-limit percentages, and not ChatGPT/Claude plan credit
consumption. If you're on a subscription, this tells you what the same work
would have cost at API rates, which is the useful number for deciding whether a
plan is worth it — but it is not a bill.

## License

MIT
