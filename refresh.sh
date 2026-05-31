#!/usr/bin/env bash
# Refresh usage.json and push to MaxGhenis/usage-data on GitHub.
# Run periodically via launchd / cron.

set -euo pipefail

cd "$HOME/usage-tracker"

export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"

# Pull latest first to avoid push conflicts
git pull --quiet --rebase origin main || true

# Sync Logpile so its SQLite ledger includes today's sessions before we
# aggregate. Non-fatal: if it hiccups we publish from slightly stale (but
# never wrong) data rather than failing the run.
"$HOME/logpile/.venv/bin/logpile" sync > /tmp/usage-tracker-refresh.log 2>&1 || true

# Generate fresh usage.json from logpile.db
python3 build_usage.py >> /tmp/usage-tracker-refresh.log 2>&1

# Commit only if there were changes
if ! git diff --quiet -- usage.json; then
  git add usage.json
  git -c user.email=max@policyengine.org -c user.name="Max Ghenis" \
    commit -q -m "Refresh usage.json $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push --quiet origin main
fi
