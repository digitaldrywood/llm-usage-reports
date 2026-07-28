#!/usr/bin/env bash
# Refresh the rolling usage report. Safe to run on a schedule (cron/systemd).
#
# Collects a rolling 30-day window ending today from every machine in
# config.json and regenerates reports/llm-usage-latest.html + reports/index.html.
# It does NOT publish anything — if you want the HTML to go somewhere, set
# "publishCommand" in config.json to your own script.
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

# Single-flight. A normal run takes ~12s, but a wedged SSH to a remote machine
# could outlast the interval; overlapping runs would race on raw/*.json.
# -n = fail immediately rather than queue up a backlog of waiting runs.
LOCK="$BASE/.ccusage/.report.lock"
if [ "${REPORT_LOCK_HELD:-}" != "1" ]; then
  export REPORT_LOCK_HELD=1
  # Not `exec flock ...` — exec replaces this shell, so the skip branch below
  # would never run and a lock conflict would exit non-zero with no log line.
  # -E 99 gives conflicts a distinct code so they can't be confused with the
  # inner script genuinely failing.
  set +e
  flock -n -E 99 "$LOCK" "$0" "$@"
  rc=$?
  set -e
  if [ "$rc" -eq 99 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') skipped: previous run still holding the lock ==="
    exit 0
  fi
  exit "$rc"
fi

# cron runs with a bare PATH — make npx/node/ssh resolvable.
export PATH="$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -x "$HOME/.npm-global/bin/npx" ] && export NPX_BIN="$HOME/.npm-global/bin/npx"

mkdir -p logs
# Frequent runs add up; keep the tail of the log rather than growing forever.
if [ -f logs/refresh.log ] && [ "$(wc -l < logs/refresh.log)" -gt 5000 ]; then
  tail -n 2000 logs/refresh.log > logs/refresh.log.tmp \
    && mv logs/refresh.log.tmp logs/refresh.log
fi
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run ==="

# 1. Stop before collecting if pricing, classification, or precision
#    regressions are detected.
python3 -m unittest discover -s scripts -p 'test_*.py'

# 2. Collect, render, rebuild the index, and run publishCommand if configured.
# -u: unbuffered, so our prints and the publish hook's output interleave
# in real order when this is piped to a log file.
python3 -u scripts/generate_report.py --collect --days 30 "$@"
