#!/bin/bash
# Cloud sessions on this repo: the job-apply server's dependencies, with the dev tools
# (pytest, ruff, mypy), ready before the session starts. Local sessions skip it.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR/plugins/job-apply/server"
uv sync --extra dev --quiet
# the tests' browser: the one cloud containers carry, not a download
if [ -n "${CLAUDE_ENV_FILE:-}" ] && [ -x /opt/pw-browsers/chromium ]; then
  echo 'export JOB_APPLY_CHROMIUM_PATH=/opt/pw-browsers/chromium' >> "$CLAUDE_ENV_FILE"
fi
