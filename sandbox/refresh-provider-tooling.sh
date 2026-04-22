#!/usr/bin/env bash
set -u

TOOLS_ROOT="${CTF_AGENT_RUNTIME_TOOLS_DIR:-/challenge/runtime-tools}"
NPM_PREFIX="${CTF_AGENT_RUNTIME_TOOLS_NPM_PREFIX:-${TOOLS_ROOT}/npm}"
PYTHON_TARGET="${CTF_AGENT_RUNTIME_TOOLS_PYTHONPATH:-${TOOLS_ROOT}/python}"
AUTO_UPDATE_RAW="${CTF_AGENT_RUNTIME_TOOLS_AUTO_UPDATE:-1}"
REFRESH_INTERVAL_RAW="${CTF_AGENT_RUNTIME_TOOLS_REFRESH_INTERVAL_SECONDS:-86400}"
STAMP_PATH="${TOOLS_ROOT}/.refresh-stamp"
LOCK_DIR="${TOOLS_ROOT}/.refresh-lock"
MANIFEST_PATH="${TOOLS_ROOT}/manifest.json"

is_truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

current_ts() {
  date +%s
}

read_stamp() {
  if [[ ! -f "${STAMP_PATH}" ]]; then
    echo 0
    return
  fi
  local raw
  raw="$(cat "${STAMP_PATH}" 2>/dev/null || echo 0)"
  if [[ "${raw}" =~ ^[0-9]+$ ]]; then
    echo "${raw}"
    return
  fi
  echo 0
}

emit_manifest() {
  python3 - <<'PY' > "${MANIFEST_PATH}" 2>/dev/null || true
import importlib.metadata
import json
import os
import subprocess
import time

def command_output(argv):
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=20)
    except Exception:
        return ""
    text = (completed.stdout or completed.stderr or "").strip()
    return text.splitlines()[0].strip()

payload = {
    "updated_at": int(time.time()),
    "codex": command_output(["codex", "--version"]),
    "gemini": command_output(["gemini", "--version"]),
    "claude_agent_sdk": "",
}
try:
    payload["claude_agent_sdk"] = importlib.metadata.version("claude-agent-sdk")
except Exception:
    payload["claude_agent_sdk"] = ""
print(json.dumps(payload, ensure_ascii=True, indent=2))
PY
}

mkdir -p "${TOOLS_ROOT}" "${NPM_PREFIX}" "${PYTHON_TARGET}"

if ! is_truthy "${AUTO_UPDATE_RAW}"; then
  emit_manifest
  echo "provider-tooling: auto-update disabled"
  exit 0
fi

REFRESH_INTERVAL=86400
if [[ "${REFRESH_INTERVAL_RAW}" =~ ^[0-9]+$ ]]; then
  REFRESH_INTERVAL="${REFRESH_INTERVAL_RAW}"
fi

NOW="$(current_ts)"
LAST_REFRESH="$(read_stamp)"
if (( REFRESH_INTERVAL > 0 && NOW - LAST_REFRESH < REFRESH_INTERVAL )); then
  emit_manifest
  echo "provider-tooling: fresh cache"
  exit 0
fi

LOCK_ACQUIRED=0
for _attempt in $(seq 1 30); do
  if mkdir "${LOCK_DIR}" 2>/dev/null; then
    LOCK_ACQUIRED=1
    break
  fi
  sleep 1
done

if (( LOCK_ACQUIRED == 0 )); then
  emit_manifest
  echo "provider-tooling: refresh skipped because another container is already updating"
  exit 0
fi

cleanup() {
  rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

NOW="$(current_ts)"
LAST_REFRESH="$(read_stamp)"
if (( REFRESH_INTERVAL > 0 && NOW - LAST_REFRESH < REFRESH_INTERVAL )); then
  emit_manifest
  echo "provider-tooling: fresh cache"
  exit 0
fi

status=0

if ! npm install -g --prefix "${NPM_PREFIX}" --no-fund --no-audit --silent \
  "@openai/codex@latest" "@google/gemini-cli@latest"; then
  status=$?
fi

if ! python3 -m pip install \
  --disable-pip-version-check \
  --no-cache-dir \
  --quiet \
  --upgrade \
  --target "${PYTHON_TARGET}" \
  claude-agent-sdk; then
  status=$?
fi

if (( status != 0 )); then
  emit_manifest
  echo "provider-tooling: refresh failed" >&2
  exit "${status}"
fi

printf '%s\n' "${NOW}" > "${STAMP_PATH}"
emit_manifest
echo "provider-tooling: refreshed"
