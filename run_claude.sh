#!/usr/bin/env bash
set -euo pipefail

export NVM_DIR=/usr/local/nvm
if [ -s "$NVM_DIR/nvm.sh" ]; then
  . "$NVM_DIR/nvm.sh"
  nvm use 20.11.1 >/dev/null
fi

# Set these in your environment or `.env` if you use a custom Claude-compatible proxy.
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  export ANTHROPIC_BASE_URL
fi
if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  export ANTHROPIC_AUTH_TOKEN
fi
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

default_model="${CLAUDE_DEFAULT_MODEL:-claude-sonnet-4-6}"
default_model_args=(--model "$default_model")
default_permission_args=(
  --permission-mode acceptEdits
  --allowedTools Agent,Bash,Edit,Glob,Grep,NotebookEdit,NotebookRead,Read,Task,TodoWrite,WebFetch,Write
)
for arg in "$@"; do
  if [[ "$arg" == "--model" || "$arg" == --model=* ]]; then
    default_model_args=()
  fi
  if [[ "$arg" == "--permission-mode" || "$arg" == --permission-mode=* ]]; then
    default_permission_args=()
  fi
  if [[ "$arg" == "--allowedTools" || "$arg" == --allowedTools=* || "$arg" == "--allowed-tools" || "$arg" == --allowed-tools=* ]]; then
    default_permission_args=()
  fi
done

cmd=(claude)
if ((${#default_model_args[@]})); then
  cmd+=("${default_model_args[@]}")
fi
if ((${#default_permission_args[@]})); then
  cmd+=("${default_permission_args[@]}")
fi
cmd+=("$@")

exec "${cmd[@]}"
