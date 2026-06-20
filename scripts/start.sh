#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

mkdir -p data/txt data/doc data/pdf
mkdir -p .docsearch

start_open_helper() {
  local pid_file=".docsearch/open-helper.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "Local file open helper is already running."
    return
  fi

  local python_bin=""
  if command -v python3 >/dev/null 2>&1; then
    python_bin="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    python_bin="$(command -v python)"
  fi

  if [[ -z "$python_bin" ]]; then
    echo "Python was not found. The web app will still run, but the local file open button needs scripts/open-helper.py." >&2
    return
  fi

  OPEN_HELPER_DATA_ROOT="$project_root/data" \
  OPEN_HELPER_HOST="127.0.0.1" \
  OPEN_HELPER_PORT="8765" \
  OPEN_HELPER_ALLOWED_ORIGINS="http://localhost:8517,http://127.0.0.1:8517" \
    nohup "$python_bin" "$project_root/scripts/open-helper.py" \
      > "$project_root/.docsearch/open-helper.out.log" \
      2> "$project_root/.docsearch/open-helper.err.log" &
  echo "$!" > "$pid_file"
  echo "Started local file open helper on http://127.0.0.1:8765."
}

start_open_helper

echo "Starting API OCR stack."
docker compose up -d --build
