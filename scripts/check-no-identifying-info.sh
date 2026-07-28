#!/usr/bin/env bash
# Guard for a public repo whose driving config is deliberately private.
#
# Scans every *tracked* file for the shapes of things that must never be
# committed: SSH targets, private-range/Tailscale IPs, real email addresses,
# and API-key prefixes. Gitignored files (config.json, .envrc, *.local.sh,
# raw/, reports/) are not scanned — they're where this detail is supposed to
# live.
#
# Run by pre-commit and by CI. A push is not undoable; this is.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail=0
report() {
  fail=1
  echo "ERROR: $1"
  echo "$2"
  echo
}

files() { git ls-files -z; }

# SSH targets (user@host) outside the documented placeholders.
if hits=$(files | xargs -0 grep -InE '[a-z_][a-z0-9_-]*@[a-z0-9.-]+\.[a-z]{2,}' 2>/dev/null \
    | grep -viE 'you@|user@|your-host|example\.(com|org)|users\.noreply\.github\.com|noreply@anthropic\.com|@[0-9]' \
    || true); [ -n "$hits" ]; then
  report "possible SSH target or email address in a tracked file" "$hits"
fi

# Tailscale CGNAT (100.64/10) and RFC1918 addresses.
if hits=$(files | xargs -0 grep -InE '\b(100\.(6[4-9]|[7-9][0-9]|1[0-2][0-9])|10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b' 2>/dev/null || true); [ -n "$hits" ]; then
  report "possible private/Tailscale IP address in a tracked file" "$hits"
fi

# Credential prefixes.
if hits=$(files | xargs -0 grep -InE '(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|whsec_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})' 2>/dev/null || true); [ -n "$hits" ]; then
  report "possible credential in a tracked file" "$hits"
fi

if [ "$fail" -ne 0 ]; then
  echo "Keep machine- and person-specific detail in config.json or .envrc (both gitignored)."
  exit 1
fi
echo "no identifying info found in tracked files"
