#!/usr/bin/env python3
"""Generate LLM usage reports from ccusage data for both dev machines.

Usage:
  generate_report.py --collect [--days N]   # collect rolling window + render latest + index
  generate_report.py --index-only           # just rebuild reports/index.html

Defaults to a rolling 30-day window ending *today*, written to
reports/llm-usage-latest.html. Today is a partial day and is excluded from
the average and the day rankings. Hand-made dated milestone reports
(reports/llm-usage-summary-<date>.html) are left untouched and linked
from the index.

See CLAUDE.md for the data-collection conventions this automates.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw")
REPORTS = os.path.join(ROOT, "reports")
PRICING_CONFIG = os.path.join(ROOT, ".ccusage", "ccusage.json")
FAST_INCIDENTS = os.path.join(ROOT, ".ccusage", "codex-fast-incidents.json")
CONFIG = os.path.join(ROOT, "config.json")
CONFIG_SAMPLE = os.path.join(ROOT, "config.sample.json")
CCUSAGE = "ccusage@20.0.17"


def load_config(path: str = None) -> dict:
    """Read the site-specific config.

    Everything that identifies a particular setup — machine names, SSH targets,
    timezone, what to do with the finished HTML — lives here rather than in the
    code, so this repo is usable by anyone without editing a .py file.
    config.json is gitignored; config.sample.json is the committed template.
    """
    path = path or CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"No config found at {path}.\n"
            f"Copy the template and edit it:  cp {os.path.basename(CONFIG_SAMPLE)} "
            f"{os.path.basename(CONFIG)}")

    machines = cfg.get("machines") or []
    if not machines:
        raise SystemExit(f"{path}: 'machines' must list at least one machine.")
    ids = [m.get("id") for m in machines]
    if len(set(ids)) != len(ids) or not all(ids):
        raise SystemExit(f"{path}: every machine needs a unique non-empty 'id'.")
    local = [m for m in machines if not m.get("ssh")]
    if len(local) != 1:
        raise SystemExit(
            f"{path}: exactly one machine must omit 'ssh' (the one you run this on); "
            f"found {len(local)}.")
    cfg.setdefault("timezone", "America/Chicago")
    cfg.setdefault("publishCommand", None)
    for m in machines:
        m.setdefault("label", m["id"])
        m.setdefault("remoteShell", "zsh -lc")
    return cfg


def raw_path(machine_id: str) -> str:
    return os.path.join(RAW, f"ccusage-{machine_id}-rolling.json")

MODEL_GROUPS = [
    ("fable5", "Fable 5", "#e3b341"),
    ("gpt55", "GPT-5.5 (Codex)", "#4f8cc9"),
    ("gpt56sol", "GPT-5.6 Sol (Codex)", "#79c0ff"),
    ("opus5", "Opus 5", "#ffc4a3"),
    ("opus48", "Opus 4.8", "#f0a07a"),
    ("opus47", "Opus 4.7", "#d97757"),
    ("opusother", "Other Claude (Opus 4.5/4.6)", "#b86b4b"),
    ("sonnet5", "Sonnet 5", "#a371f7"),
    ("sonnet46", "Sonnet 4.6", "#7c5cbf"),
    ("haiku45", "Haiku 4.5", "#57a773"),
    ("gptother", "Other GPT (Codex)", "#38618c"),
    ("gemini", "Gemini", "#4285f4"),
    ("other", "Other", "#8b949e"),
]
GLABEL = {k: lbl for k, lbl, _ in MODEL_GROUPS}
GCOLOR = {k: c for k, _, c in MODEL_GROUPS}

AGENTS = [
    ("claude", "Claude", "#d97757"),
    ("codex", "Codex (GPT)", "#4f8cc9"),
    ("gemini", "Gemini", "#4285f4"),
    ("other", "Other", "#8b949e"),
]
ALABEL = {k: lbl for k, lbl, _ in AGENTS}
ACOLOR = {k: color for k, _, color in AGENTS}


def normalize_model(model: str) -> str:
    """Strip ccusage's bracketed agent prefix from a model name.

    Some sources report models as "[openclaw] gpt-5.4" rather than "gpt-5.4".
    Left unhandled, that spend lands in Other/Other instead of its real model
    group and provider.
    """
    return re.sub(r"^\[[^\]]+\]\s*", "", model)


def group_of(model: str) -> str:
    model = normalize_model(model)
    if model.startswith("gpt-5.5"):
        return "gpt55"
    if model.startswith("gpt-5.6"):
        return "gpt56sol"
    if model.startswith("gpt"):
        return "gptother"
    # Match the bare alias and any dated snapshot (claude-opus-5-20260701),
    # but never a future minor line (claude-opus-5-1 would need its own group).
    if model == "claude-opus-5" or re.fullmatch(r"claude-opus-5-\d{8}", model):
        return "opus5"
    if model == "claude-opus-4-8":
        return "opus48"
    if model == "claude-opus-4-7":
        return "opus47"
    if model.startswith("claude-opus"):
        return "opusother"
    if model == "claude-fable-5":
        return "fable5"
    if model.startswith("claude-haiku"):
        return "haiku45"
    if model.startswith("claude-sonnet-4"):
        return "sonnet46"
    if model.startswith("claude-sonnet"):
        return "sonnet5"
    if model.startswith("gemini"):
        return "gemini"
    return "other"


def agent_of(model: str) -> str:
    """Map a raw model name to its provider/agent family."""
    model = normalize_model(model)
    if model.startswith("gpt"):
        return "codex"
    if model.startswith("claude"):
        return "claude"
    if model.startswith("gemini"):
        return "gemini"
    return "other"


def run(cmd: list, **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def _codex_agent_gpt_rows(day: dict):
    """GPT model rows that came from the Codex CLI source on this day.

    Requires the unified payload to have been collected with ``--by-agent``.
    GPT usage also shows up under the ``claude`` agent when Claude Code
    delegates to Codex via the codex plugin; those rows are a different source
    and must not be reconciled against ``ccusage codex daily``.
    """
    if "agents" not in day:
        raise RuntimeError(
            f'unified payload for {day["period"]} lacks per-agent data; '
            "collect the unified report with --by-agent")
    for agent in day["agents"]:
        if agent.get("agent") != "codex":
            continue
        for mb in agent.get("modelBreakdowns", []):
            if mb["modelName"].startswith("gpt"):
                yield mb


def _merge_agent_breakdowns(day: dict) -> list:
    """Rebuild the day's top-level modelBreakdowns from its per-agent rows."""
    merged: dict = {}
    order: list = []
    for agent in day["agents"]:
        for mb in agent.get("modelBreakdowns", []):
            name = mb["modelName"]
            if name not in merged:
                merged[name] = {"modelName": name}
                order.append(name)
            for key, value in mb.items():
                if key == "modelName":
                    continue
                merged[name][key] = merged[name].get(key, 0) + value
    return [merged[name] for name in order]


def normalize_codex_standard(unified_json: str, codex_json: str,
                             live_date: str = None) -> str:
    """Replace Codex-CLI GPT costs with the explicit standard-speed costs.

    ccusage's unified ``daily`` command has ignored ``codex.defaults.speed``
    and may read the machine's present-day Codex service tier. That can
    retroactively multiply every historical GPT cost by the fast-mode credit
    factor. The source-specific Codex command honors ``--speed standard``, so
    use its daily totals to normalize the unified model breakdown without
    changing token counts.

    Only the ``codex`` agent's GPT rows are reconciled. Since 2026-07-19 the
    ``claude`` agent also reports GPT usage (Claude Code delegating to Codex
    through the codex plugin); ``ccusage codex daily`` never sees that source,
    so folding it into the comparison produced a false Fast-mode detection and
    then a hard reconciliation failure.

    ``live_date`` (today, when the window ends on today) is excluded from both
    the Fast-mode comparison and the strict per-day check. The unified pass and
    the Codex pass are separate ccusage invocations run seconds apart, so any
    agent still writing logs makes today's two readings legitimately differ.
    Reconciling a day that is still being written cannot succeed; it settles on
    the next run after midnight.
    """
    unified = json.loads(unified_json)
    codex = json.loads(codex_json)
    standard_by_date = {day["date"]: day["costUSD"] for day in codex["daily"]}

    def settled(period):
        return period != live_date

    current_gpt = sum(
        mb["cost"] for day in unified["daily"] if settled(day["period"])
        for mb in _codex_agent_gpt_rows(day))
    expected_gpt = sum(cost for date, cost in standard_by_date.items()
                       if settled(date))
    using_fast = abs(current_gpt - expected_gpt) >= 0.005

    def fast_multiplier(model: str) -> float:
        # ccusage 20.0.17 assigns GPT-5.5 a 2.5x fast multiplier. Other GPT
        # models currently use its 2x Codex fallback. Reconciliation below is
        # deliberately strict so a future package change fails closed.
        return 2.5 if model.startswith("gpt-5.5") else 2.0

    delegated_gpt = 0.0
    for day in unified["daily"]:
        gpt_rows = list(_codex_agent_gpt_rows(day))
        standard = standard_by_date.get(day["period"], 0.0)
        if standard and not gpt_rows and settled(day["period"]):
            raise RuntimeError(
                f'Codex standard cost exists without GPT breakdown on {day["period"]}')
        if using_fast:
            for mb in gpt_rows:
                mb["cost"] /= fast_multiplier(mb["modelName"])
        calculated = sum(mb["cost"] for mb in gpt_rows)
        if settled(day["period"]) and abs(calculated - standard) >= 0.005:
            raise RuntimeError(
                f'Codex daily normalization mismatch on {day["period"]}: '
                f"expected={standard:.6f} calculated={calculated:.6f}")
        delegated_gpt += sum(
            mb["cost"] for agent in day["agents"] if agent.get("agent") != "codex"
            for mb in agent.get("modelBreakdowns", [])
            if mb["modelName"].startswith("gpt"))
        day["modelBreakdowns"] = _merge_agent_breakdowns(day)
        day["totalCost"] = sum(mb["cost"] for mb in day["modelBreakdowns"])

    unified["totals"]["totalCost"] = sum(day["totalCost"] for day in unified["daily"])
    normalized_gpt = sum(
        mb["cost"] for day in unified["daily"] if settled(day["period"])
        for mb in _codex_agent_gpt_rows(day))
    if abs(normalized_gpt - expected_gpt) >= 0.005:
        raise RuntimeError(
            f"Codex normalization mismatch: expected={expected_gpt:.6f} "
            f"calculated={normalized_gpt:.6f}")
    unified["_llmUsageReport"] = {
        "codexFastPricingDetected": using_fast,
        "codexCostBeforeNormalization": current_gpt,
        "codexStandardCost": expected_gpt,
        # GPT spend logged by Claude Code rather than the Codex CLI. Still
        # counted as Codex in the agent split, but priced from Claude Code's
        # own logs and therefore outside the standard-speed reconciliation.
        "delegatedGptCost": delegated_gpt,
    }
    return json.dumps(unified, separators=(",", ":"))


def update_fast_incidents(speed_by_machine: dict, report_since=None,
                          report_until=None, on_date=None):
    """Open/resolve durable Fast-mode incidents from this collection run."""
    today = on_date or dt.date.today().isoformat()
    try:
        with open(FAST_INCIDENTS, encoding="utf-8") as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {"incidents": []}
    incidents = history.setdefault("incidents", [])
    for machine, detected in speed_by_machine.items():
        open_incident = next(
            (item for item in reversed(incidents)
             if item["machine"] == machine and item.get("resolvedOn") is None),
            None)
        if detected and open_incident is None:
            incidents.append({
                "machine": machine,
                "detectedOn": today,
                "resolvedOn": None,
                "observedReportFrom": report_since or today,
                "observedReportThrough": report_until or today,
                "evidence": "ccusage unified cost exceeded explicit standard-speed cost",
            })
        elif detected:
            open_incident["observedReportFrom"] = min(
                open_incident.get("observedReportFrom", report_since or today),
                report_since or today)
            open_incident["observedReportThrough"] = max(
                open_incident.get("observedReportThrough", report_until or today),
                report_until or today)
        elif not detected and open_incident is not None:
            open_incident["resolvedOn"] = today

    tmp = FAST_INCIDENTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
        f.write("\n")
    os.replace(tmp, FAST_INCIDENTS)


def collect_machine(machine: dict, since: str, until: str, tz: str,
                    live_date: str = None) -> str:
    """Run both ccusage passes for one machine and return normalized JSON.

    A machine with no 'ssh' target runs locally; anything else is driven over
    SSH, with the pricing config copied across first so both ends compute
    identical prices.
    """
    npx = os.environ.get("NPX_BIN", "npx")
    ssh_target = machine.get("ssh")

    if not ssh_target:
        unified = run([npx, "-y", CCUSAGE, "daily", "--since", since, "--until", until,
                       "--timezone", tz, "--breakdown", "--by-agent", "--json",
                       "--offline", "--config", PRICING_CONFIG])
        codex = run([npx, "-y", CCUSAGE, "codex", "daily", "--since", since,
                     "--until", until, "--timezone", tz, "--json", "--offline",
                     "--speed", "standard", "--config", PRICING_CONFIG])
        return normalize_codex_standard(unified, codex, live_date)

    shell = machine["remoteShell"]
    remote_config = "/tmp/llm-usage-ccusage.json"
    run(["scp", "-q", "-o", "ConnectTimeout=20", PRICING_CONFIG,
         f"{ssh_target}:{remote_config}"])
    try:
        remote = (f'{shell} "npx -y {CCUSAGE} daily --since {since} --until {until} '
                  f'--timezone {tz} --breakdown --by-agent --json --offline '
                  f'--config {remote_config}"')
        unified = run(["ssh", "-o", "ConnectTimeout=20", ssh_target, remote])
        remote_codex = (f'{shell} "npx -y {CCUSAGE} codex daily --since {since} '
                        f'--until {until} --timezone {tz} --json --offline '
                        f'--speed standard --config {remote_config}"')
        codex = run(["ssh", "-o", "ConnectTimeout=20", ssh_target, remote_codex])
        return normalize_codex_standard(unified, codex, live_date)
    finally:
        subprocess.run(["ssh", "-o", "ConnectTimeout=20", ssh_target,
                        f"rm -f {remote_config}"], capture_output=True, text=True)


def collect(cfg: dict, since: str, until: str, live_date: str = None) -> dict:
    """Run ccusage on every configured machine, writing rolling raw files.

    Returns {machine_id: path}. Every machine is collected and validated before
    any snapshot is replaced, so one unreachable machine leaves the previous
    good report intact rather than publishing a partial total.
    """
    payloads = {m["id"]: collect_machine(m, since, until, cfg["timezone"], live_date)
                for m in cfg["machines"]}
    for content in payloads.values():
        json.loads(content)

    staged = []
    try:
        for mid, content in payloads.items():
            tmp = raw_path(mid) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            staged.append((tmp, raw_path(mid)))
        for tmp, path in staged:
            os.replace(tmp, path)
        speed_by_machine = {
            mid: json.loads(content)["_llmUsageReport"]["codexFastPricingDetected"]
            for mid, content in payloads.items()}
        update_fast_incidents(speed_by_machine, since, until)
        for machine, detected in speed_by_machine.items():
            if detected:
                print(f"WARNING: Codex Fast pricing detected on {machine}; "
                      "recorded incident and normalized report to Standard")
    finally:
        for tmp, _ in staged:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return {mid: raw_path(mid) for mid in payloads}


def load(files: dict, dates: list):
    """Merge per-day per-model across machines. files = {machine: path}."""
    modelcost = defaultdict(lambda: defaultdict(float))   # date -> grp -> cost
    agentcost = defaultdict(lambda: defaultdict(float))   # date -> agent -> cost
    machine_total = defaultdict(lambda: defaultdict(float))
    source_reported_total = defaultdict(float)
    tokens = defaultdict(float)
    daytokens = defaultdict(float)                         # date -> total tokens
    unclassified: set = set()
    unpriced: set = set()
    for mach, path in files.items():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        source_reported_total[mach] = data["totals"]["totalCost"]
        for day in data["daily"]:
            date = day["period"]
            tokens["cacheRead"] += day["cacheReadTokens"]
            tokens["output"] += day["outputTokens"]
            tokens["total"] += day["totalTokens"]
            daytokens[date] += day["totalTokens"]
            for mb in day["modelBreakdowns"]:
                name = mb["modelName"]
                grp = group_of(name)
                agent = agent_of(name)
                # A model released after the last MODEL_GROUPS update lands in
                # "Other"; one missing from the pricing snapshot costs $0
                # despite real tokens. Both are silent undercounts, so surface
                # them instead of publishing a quietly wrong chart.
                if grp == "other" or agent == "other":
                    unclassified.add(name)
                # Only hosted providers we price can be "missing a price".
                # Local models (Ollama, e.g. llama3.2:3b) are legitimately $0,
                # so warning on those would cry wolf every run.
                if agent != "other" and mb["cost"] == 0 and mb.get(
                        "totalTokens", sum(
                            mb.get(k, 0) for k in ("inputTokens", "outputTokens",
                                                   "cacheReadTokens",
                                                   "cacheCreationTokens"))):
                    unpriced.add(name)
                modelcost[date][grp] += mb["cost"]
                agentcost[date][agent] += mb["cost"]
                machine_total[mach][agent] += mb["cost"]
                machine_total[mach]["total"] += mb["cost"]

    for name in sorted(unclassified):
        print(f"WARNING: {name} is not in MODEL_GROUPS/agent_of; charted as "
              '"Other". Add it to scripts/generate_report.py.')
    for name in sorted(unpriced):
        print(f"WARNING: {name} reports tokens but $0 cost; ccusage's offline "
              "pricing snapshot is missing it. Add a pricingOverrides entry to "
              ".ccusage/ccusage.json.")

    incidents = []
    if dates:
        try:
            with open(FAST_INCIDENTS, encoding="utf-8") as f:
                history = json.load(f)
        except FileNotFoundError:
            history = {"incidents": []}
        since, until = dates[0], dates[-1]
        incidents = []
        for item in history.get("incidents", []):
            incident_since = item.get("observedReportFrom", item["detectedOn"])
            incident_until = item.get(
                "observedReportThrough", item.get("resolvedOn") or until)
            if incident_since <= until and incident_until >= since:
                incidents.append(item)

    # Preserve full precision for all calculations. Round only while formatting
    # visible currency values in the HTML.
    arrays = {g: [modelcost[d].get(g, 0.0) for d in dates] for g, _, _ in MODEL_GROUPS}
    agents = {a: [agentcost[d].get(a, 0.0) for d in dates] for a, _, _ in AGENTS}
    model_totals = {g: sum(arrays[g]) for g, _, _ in MODEL_GROUPS}
    grand = sum(model_totals.values())
    day_tokens = [daytokens[d] for d in dates]
    return dict(arrays=arrays, agents=agents, model_totals=model_totals,
                grand=grand, machine_total=machine_total,
                source_reported_total=source_reported_total,
                tokens=tokens, day_tokens=day_tokens, dates=dates,
                fast_incidents=incidents)


def usd(v):
    return "$" + format(v, ",.2f")


def usd0(v):
    return "$" + format(round(v), ",")


def summary_usd(v):
    return usd(v) if 0 < v < 1 else usd0(v)


def render_report(d: dict, period_label: str, refreshed: str = "",
                  cfg: dict = None) -> str:
    cfg = cfg or {"machines": [], "timezone": "America/Chicago"}
    machines = cfg["machines"]
    machine_names = " + ".join(m["label"] for m in machines)
    dates = d["dates"]
    labels = [f"{int(x[5:7])}/{int(x[8:10])}" for x in dates]
    daily_total = [sum(d["agents"][a][i] for a, _, _ in AGENTS) for i in range(len(dates))]
    grand = d["grand"]
    agent_totals = {a: sum(d["agents"][a]) for a, _, _ in AGENTS}
    active_agents = [a for a, _, _ in AGENTS if agent_totals[a] > 0]
    claude_total = agent_totals["claude"]
    codex_total = agent_totals["codex"]
    n = len(dates)
    # The window now ends on today, which is still accumulating. Averaging and
    # ranking over it would drag the average down all morning and make "today"
    # the quietest day almost every run, so both use completed days only.
    complete = list(range(n - 1)) if n > 1 else list(range(n))
    avg = (sum(daily_total[i] for i in complete) / len(complete)) if complete else 0
    # peak day
    peak_i = max(complete, key=lambda i: daily_total[i]) if complete else 0
    peak_lbl = labels[peak_i] if n else ""
    cache_pct = round(100 * d["tokens"]["cacheRead"] / d["tokens"]["total"], 1) if d["tokens"]["total"] else 0

    # Fast-mode incidents are no longer rendered as a page banner (they pushed
    # the actual data below the fold). The guard itself is unchanged: incidents
    # are still recorded in .ccusage/codex-fast-incidents.json and surfaced on
    # stdout, so the cron log stays the alerting channel.
    for item in d.get("fast_incidents", []):
        if item.get("resolvedOn") is None:
            print(f'WARNING: unresolved Codex Fast-mode incident on {item["machine"]} '
                  f'(detected {item["detectedOn"]}); dollar figures are normalized to '
                  "Standard but ChatGPT credit burn may be higher. Use /fast off.")

    # ordered model rows by total desc
    ordered = sorted(MODEL_GROUPS, key=lambda g: d["model_totals"][g[0]], reverse=True)
    ordered = [g for g in ordered if d["model_totals"][g[0]] > 0]

    model_rows = "\n".join(
        f'        <tr><td>{lbl.replace(" (Codex)", " (Codex)")}</td><td>{usd(d["model_totals"][k])}</td>'
        f'<td>{round(100*d["model_totals"][k]/grand,1) if grand else 0}%</td></tr>'
        for k, lbl, _ in ordered)

    # daily-model datasets (ordered desc by total)
    def jsarr(a):
        return "[" + ",".join(f"{x:.6f}" for x in a) + "]"

    def jsintarr(a):
        return "[" + ",".join(str(int(x)) for x in a) + "]"

    model_consts = "\n".join(f"const {k} = {jsarr(d['arrays'][k])};" for k, _, _ in ordered)
    model_datasets = ",\n    ".join(
        f"{{ label: '{lbl}', data: {k}, backgroundColor: '{GCOLOR[k]}' }}"
        for k, lbl, _ in ordered)
    split_labels = ", ".join(
        f"'{lbl.replace(' (Codex)', '')} — {summary_usd(d['model_totals'][k])}'"
        for k, lbl, _ in ordered)
    split_data = ", ".join(f"{d['model_totals'][k]:.6f}" for k, _, _ in ordered)
    split_colors = ", ".join(f"'{GCOLOR[k]}'" for k, _, _ in ordered)

    agent_consts = "\n".join(
        f"const agent_{a} = {jsarr(d['agents'][a])};" for a in active_agents)
    agent_datasets = ",\n    ".join(
        f"{{ label: '{ALABEL[a]}', data: agent_{a}, backgroundColor: '{ACOLOR[a]}' }}"
        for a in active_agents)
    agent_split_labels = ", ".join(f"'{ALABEL[a]}'" for a in active_agents)
    agent_split_data = ", ".join(f"{agent_totals[a]:.6f}" for a in active_agents)
    agent_split_colors = ", ".join(f"'{ACOLOR[a]}'" for a in active_agents)
    extra_agent_cards = "\n".join(
        f'    <div class="card"><div class="label">{ALABEL[a]}</div>'
        f'<div class="value">{summary_usd(agent_totals[a])}</div>'
        f'<div class="note">{round(100*agent_totals[a]/grand,1) if grand else 0}% of spend</div></div>'
        for a in ("gemini", "other") if agent_totals[a] > 0)

    # by-machine, driven by the configured machine list rather than fixed names
    mt = d["machine_total"]
    machine_headers = "".join(f"<th>{ALABEL[a]}</th>" for a in active_agents)

    def machine_row(label, values):
        cells = "".join(f'<td>{usd(values.get(a, 0))}</td>' for a in active_agents)
        return f'        <tr><td>{label}</td>{cells}<td>{usd(values.get("total",0))}</td></tr>'

    machine_rows = "\n".join(
        machine_row(m["label"], mt.get(m["id"], {})) for m in machines)
    combined_agent_cells = "".join(f"<td>{usd(agent_totals[a])}</td>" for a in active_agents)

    # highlights: top 3 + quietest, over completed days only
    order_days = sorted(complete, key=lambda i: daily_total[i], reverse=True)
    hi = []
    rank_lbl = ["Biggest day", "#2 day", "#3 day"]
    for r in range(min(3, len(order_days))):
        i = order_days[r]
        hi.append(f'        <tr><td>{rank_lbl[r]} &mdash; {dates[i][5:]}</td><td>{usd(daily_total[i])}</td></tr>')
    if order_days:
        qi = order_days[-1]
        hi.append(f'        <tr><td>Quietest day &mdash; {dates[qi][5:]}</td><td>{usd(daily_total[qi])}</td></tr>')
    if n > 1:
        hi.append(f'        <tr><td>Today so far &mdash; {dates[-1][5:]}</td>'
                  f'<td>{usd(daily_total[-1])}</td></tr>')
    hi.append(f'        <tr><td>Cache reads (combined)</td><td>{d["tokens"]["cacheRead"]/1e9:.1f}B tokens</td></tr>')
    hi.append(f'        <tr><td>Output tokens (combined)</td><td>{d["tokens"]["output"]/1e6:.1f}M</td></tr>')
    highlight_rows = "\n".join(hi)

    top_model = ordered[0]
    refreshed_note = (f"Refreshed hourly &middot; last update {refreshed} &middot; "
                      "today's bar is still filling in" if refreshed else "")
    return TEMPLATE.format(
        title_period=period_label,
        sub_period=period_label,
        refreshed_note=refreshed_note,
        timezone=cfg['timezone'], machine_names=machine_names,
        grand=usd(grand), ndays=n,
        avg=usd0(avg), peak=usd0(daily_total[peak_i] if n else 0), peak_lbl=peak_lbl,
        claude_total=usd0(claude_total), claude_pct=round(100*claude_total/grand,1) if grand else 0,
        codex_total=usd0(codex_total), codex_pct=round(100*codex_total/grand,1) if grand else 0,
        extra_agent_cards=extra_agent_cards,
        total_tokens=f"{d['tokens']['total']/1e9:.2f}B", cache_pct=cache_pct,
        model_rows=model_rows, grand_row=usd(grand),
        machine_headers=machine_headers, machine_rows=machine_rows,
        combined_agent_cells=combined_agent_cells, combined_total=usd(grand),
        highlight_rows=highlight_rows,
        top_model_label=top_model[1].replace(" (Codex)", ""),
        top_model_total=usd0(d["model_totals"][top_model[0]]),
        labels_js=json.dumps(labels),
        agent_consts=agent_consts, agent_datasets=agent_datasets,
        day_tokens_js=jsintarr(d["day_tokens"]),
        daily_total_js=jsarr(daily_total),
        model_consts=model_consts, model_datasets=model_datasets,
        split_labels=split_labels, split_data=split_data, split_colors=split_colors,
        agent_split_labels=agent_split_labels, agent_split_data=agent_split_data,
        agent_split_colors=agent_split_colors,
    )


def build_index(cfg: dict = None):
    """Scan reports/ and write index.html linking every report in date order."""
    cfg = cfg or {"machines": []}
    machine_names = " and ".join(m["label"] for m in cfg["machines"])
    entries = []  # (sortkey, label, href, period)
    for path in glob.glob(os.path.join(REPORTS, "*.html")):
        fn = os.path.basename(path)
        if fn == "index.html":
            continue
        html = open(path, encoding="utf-8").read()
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else fn
        period = title.split("—", 1)[1].strip() if "—" in title else title
        if fn == "llm-usage-latest.html":
            entries.append(("9999-99-99", "Latest (rolling 30 days)", fn, period))
        else:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
            key = dm.group(1) if dm else fn
            entries.append((key, f"Report — generated {key}", fn, period))
    # latest first, then dated newest-first
    entries.sort(key=lambda e: e[0], reverse=True)

    rows = "\n".join(
        f'      <a class="report" href="{href}">'
        f'<span class="rlabel">{label}</span>'
        f'<span class="rperiod">{period}</span></a>'
        for _, label, href, period in entries)
    html = INDEX_TEMPLATE.format(rows=rows, count=len(entries),
                                 machine_names=machine_names)
    with open(os.path.join(REPORTS, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    return len(entries)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LLM Coding Agent Usage — {title_period}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --claude: #d97757; --codex: #4f8cc9; --gemini: #4285f4; --accent: #e3b341;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 32px 24px 64px; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); margin-bottom: 28px; }}
  .sub.refreshed {{ margin-top: -22px; font-size: 12px; opacity: .8; }}
  .navlink {{ display: inline-block; margin-bottom: 18px; color: var(--accent); text-decoration: none; font-size: 13px; }}
  .navlink:hover {{ text-decoration: underline; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 28px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }}
  .card .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
  .card .note {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
  .panel {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 24px; }}
  .panel h2 {{ font-size: 16px; margin: 0 0 14px; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 800px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: right; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .total-row td {{ font-weight: 700; border-top: 2px solid var(--border); }}
  .footnote {{ color: var(--muted); font-size: 13px; margin-top: 24px; }}
  canvas {{ max-height: 380px; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="navlink" href="index.html">&larr; All reports</a>
  <h1>LLM Coding Agent Usage</h1>
  <div class="sub">{sub_period} ({timezone}) &middot; Combined across {machine_names} &middot; via ccusage</div>
  <div class="sub refreshed">{refreshed_note}</div>

  <div class="cards">
    <div class="card"><div class="label">Total Spend</div><div class="value">{grand}</div><div class="note">{ndays} days, both machines &middot; today partial</div></div>
    <div class="card"><div class="label">Avg / Day</div><div class="value">{avg}</div><div class="note">peak {peak} on {peak_lbl} &middot; complete days only</div></div>
    <div class="card"><div class="label">Claude</div><div class="value">{claude_total}</div><div class="note">{claude_pct}% of spend</div></div>
    <div class="card"><div class="label">Codex (GPT)</div><div class="value">{codex_total}</div><div class="note">{codex_pct}% of spend</div></div>
{extra_agent_cards}
    <div class="card"><div class="label">Total Tokens</div><div class="value">{total_tokens}</div><div class="note">{cache_pct}% cache reads</div></div>
  </div>

  <div class="panel">
    <h2>Daily Spend by Agent &amp; Token Volume (both machines combined)</h2>
    <canvas id="daily"></canvas>
  </div>

  <div class="panel">
    <h2>Daily Spend by Model (both machines combined)</h2>
    <canvas id="dailyModel"></canvas>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>Model Split</h2>
      <canvas id="modelSplit"></canvas>
    </div>
    <div class="panel">
      <h2>By Model</h2>
      <table>
        <tr><th>Model</th><th>Spend</th><th>Share</th></tr>
{model_rows}
        <tr class="total-row"><td>Total</td><td>{grand_row}</td><td>100%</td></tr>
      </table>
    </div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>Agent Split</h2>
      <canvas id="split"></canvas>
    </div>
    <div class="panel">
      <h2>Cumulative Spend</h2>
      <canvas id="cumulative"></canvas>
    </div>
  </div>

  <div class="grid2">
    <div class="panel">
      <h2>By Machine</h2>
      <table>
        <tr><th>Machine</th>{machine_headers}<th>Total</th></tr>
{machine_rows}
        <tr class="total-row"><td>Combined</td>{combined_agent_cells}<td>{combined_total}</td></tr>
      </table>
    </div>
    <div class="panel">
      <h2>Highlights</h2>
      <table>
        <tr><th>Item</th><th>Value</th></tr>
{highlight_rows}
      </table>
    </div>
  </div>

  <div class="footnote">
    Per-model allocations come from <code>ccusage --breakdown</code> on each machine; totals are summed at full precision before display rounding.
    Top model this period: {top_model_label} ({top_model_total}). Codex spend covers all GPT models (gpt-5.5, gpt-5.6-sol, etc.) from both the Codex CLI and Codex work delegated from Claude Code.
    Costs are API-equivalent estimates from token counts using a pinned offline pricing snapshot &mdash; not invoices or subscription-limit percentages.
    Fable 5 rates are already 2&times; Opus 4.8; no extra multiplier is applied. ChatGPT fast-mode credit consumption is a separate plan metric and is excluded here.
    Generated from the saved per-machine raw snapshots.
  </div>
</div>

<script>
const labels = {labels_js};
{agent_consts}
const dailyTotals = {daily_total_js};

const C = getComputedStyle(document.documentElement);
const mutedColor = C.getPropertyValue('--muted').trim();
const borderColor = C.getPropertyValue('--border').trim();
Chart.defaults.color = mutedColor;
Chart.defaults.borderColor = borderColor;
const usd = v => '$' + v.toLocaleString('en-US', {{maximumFractionDigits: 0}});
const tok = v => v >= 1e9 ? (v / 1e9).toFixed(1) + 'B'
               : v >= 1e6 ? Math.round(v / 1e6) + 'M'
               : v >= 1e3 ? Math.round(v / 1e3) + 'K' : v;
const tokFull = v => v.toLocaleString('en-US') + ' tokens';

const dayTokens = {day_tokens_js};

new Chart(document.getElementById('daily'), {{
  type: 'bar',
  data: {{ labels, datasets: [
    {agent_datasets},
    {{ type: 'line', label: 'Tokens', data: dayTokens, yAxisID: 'y1',
       borderColor: '#8b949e', backgroundColor: '#8b949e', borderWidth: 2,
       pointRadius: 2, pointHoverRadius: 4, tension: 0.25, fill: false, order: 0 }}
  ]}},
  options: {{
    responsive: true,
    interaction: {{ mode: 'index', intersect: false }},
    scales: {{
      x: {{ stacked: true, grid: {{ display: false }} }},
      y: {{ stacked: true, position: 'left', ticks: {{ callback: usd }},
            title: {{ display: true, text: 'Spend' }} }},
      // Tokens ride a separate right-hand scale. drawOnChartArea:false keeps
      // its gridlines from doubling up on the spend grid.
      y1: {{ position: 'right', beginAtZero: true, stacked: false,
             grid: {{ drawOnChartArea: false }}, ticks: {{ callback: tok }},
             title: {{ display: true, text: 'Tokens' }} }}
    }},
    plugins: {{ tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.yAxisID === 'y1'
      ? ctx.dataset.label + ': ' + tokFull(ctx.parsed.y)
      : ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2) }} }} }}
  }}
}});

{model_consts}

new Chart(document.getElementById('dailyModel'), {{
  type: 'bar',
  data: {{ labels, datasets: [
    {model_datasets}
  ]}},
  options: {{
    responsive: true,
    scales: {{ x: {{ stacked: true, grid: {{ display: false }} }}, y: {{ stacked: true, ticks: {{ callback: usd }} }} }},
    plugins: {{ tooltip: {{ callbacks: {{ label: ctx => ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2) }} }}, legend: {{ labels: {{ boxWidth: 14 }} }} }}
  }}
}});

new Chart(document.getElementById('modelSplit'), {{
  type: 'doughnut',
  data: {{ labels: [{split_labels}], datasets: [{{ data: [{split_data}], backgroundColor: [{split_colors}], borderColor: 'transparent' }}] }},
  options: {{ plugins: {{ legend: {{ position: 'bottom' }} }} }}
}});

new Chart(document.getElementById('split'), {{
  type: 'doughnut',
  data: {{ labels: [{agent_split_labels}], datasets: [{{ data: [{agent_split_data}], backgroundColor: [{agent_split_colors}], borderColor: 'transparent' }}] }},
  options: {{ plugins: {{ legend: {{ position: 'bottom' }} }} }}
}});

let run = 0;
const cumulative = dailyTotals.map(value => +(run += value).toFixed(6));
new Chart(document.getElementById('cumulative'), {{
  type: 'line',
  data: {{ labels, datasets: [{{ label: 'Cumulative spend', data: cumulative, borderColor: '#e3b341', backgroundColor: 'rgba(227,179,65,0.12)', fill: true, tension: 0.25, pointRadius: 0 }}] }},
  options: {{ scales: {{ y: {{ ticks: {{ callback: usd }} }}, x: {{ grid: {{ display: false }} }} }}, plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => '$' + ctx.parsed.y.toLocaleString('en-US', {{maximumFractionDigits: 2}}) }} }} }} }}
}});
</script>
</body>
</html>
"""


INDEX_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LLM Usage Reports — Index</title>
<style>
  :root {{ --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3; --muted: #8b949e; --accent: #e3b341; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 48px 24px 64px; }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); margin-bottom: 28px; }}
  .report {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 12px; text-decoration: none; color: var(--text); transition: border-color .15s; }}
  .report:hover {{ border-color: var(--accent); }}
  .rlabel {{ font-weight: 700; }}
  .rperiod {{ color: var(--muted); font-size: 13px; text-align: right; }}
  .footnote {{ color: var(--muted); font-size: 13px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>LLM Coding Agent Usage Reports</h1>
  <div class="sub">{count} report(s) &middot; API-equivalent token costs for coding agents across {machine_names}</div>
{rows}
  <div class="footnote">The "Latest" report refreshes daily (rolling 30-day window). Dated reports are fixed milestones. Costs are ccusage API-equivalent estimates using the saved pricing snapshot, not invoices, subscription-limit percentages, or ChatGPT fast-mode credits.</div>
</div>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true", help="collect fresh data + render latest report")
    ap.add_argument("--days", type=int, default=30, help="rolling window length (default 30)")
    ap.add_argument("--index-only", action="store_true", help="only rebuild index.html")
    ap.add_argument("--today", help="override 'today' as YYYY-MM-DD (for testing)")
    ap.add_argument("--config", help=f"path to config (default {CONFIG})")
    ap.add_argument("--no-publish", action="store_true",
                    help="skip the configured publishCommand")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.index_only:
        n = build_index(cfg)
        print(f"index.html rebuilt: {n} report(s)")
        return

    if args.today:
        today = dt.date.fromisoformat(args.today)
    else:
        today = dt.date.today()
    # The window ends today, not yesterday: an hourly refresh is only worth
    # publishing if today's spend is actually in it. Today is a partial day and
    # is labelled as such everywhere it would otherwise read as a full one.
    until = today
    since = until - dt.timedelta(days=args.days - 1)
    dates = [(since + dt.timedelta(days=i)).isoformat() for i in range((until - since).days + 1)]
    since_s, until_s = since.isoformat(), until.isoformat()

    machine_ids = [m["id"] for m in cfg["machines"]]
    if args.collect:
        names = ", ".join(m["label"] for m in cfg["machines"])
        print(f"Collecting {since_s} .. {until_s} from {names}...")
        files = collect(cfg, since_s, until_s, live_date=until_s)
    else:
        files = {mid: raw_path(mid) for mid in machine_ids}

    d = load(files, dates)
    # Reconciliation failures stop the publish path. A displayed cent must
    # never depend on whether we summed rounded chart points or raw costs.
    for mach, reported in d["source_reported_total"].items():
        calculated = d["machine_total"][mach]["total"]
        if abs(reported - calculated) >= 0.005:
            raise RuntimeError(
                f"{mach} breakdown mismatch: reported={reported:.6f} calculated={calculated:.6f}")
    msum = sum(d["machine_total"][m]["total"] for m in d["machine_total"])
    if abs(msum - d["grand"]) >= 0.005:
        raise RuntimeError(f"combined breakdown mismatch: grand={d['grand']:.6f} machines={msum:.6f}")

    sm = f"{since.strftime('%b %-d')} – {until.strftime('%b %-d, %Y')}"
    label = f"{sm} (rolling {args.days}d)"
    refreshed = dt.datetime.now().strftime("%b %-d, %-I:%M %p %Z").strip()
    html = render_report(d, label, refreshed, cfg)
    out = os.path.join(REPORTS, "llm-usage-latest.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out}  total={usd(d['grand'])}  ({since_s}..{until_s})")

    n = build_index(cfg)
    print(f"index.html rebuilt: {n} report(s)")

    # Publishing is deliberately not this repo's job — it only produces HTML in
    # reports/. If you want the output to go somewhere, point publishCommand at
    # your own script; it runs from the project root after a successful render.
    publish = cfg.get("publishCommand")
    if publish and not args.no_publish:
        rc = subprocess.run(publish, shell=True, cwd=ROOT).returncode
        if rc != 0:
            # The report is already written; only the hand-off failed. Exit
            # non-zero so a scheduler notices, but without a traceback that
            # buries the publish script's own error message.
            raise SystemExit(
                f"publishCommand failed (exit {rc}): {publish}\n"
                f"The report was still written to {out}.")


if __name__ == "__main__":
    main()
