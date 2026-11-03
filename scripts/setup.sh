#!/usr/bin/env bash
# Prepare a checkout or deploy one target. See docs/guides/agent-setup.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/setup.sh prepare
  scripts/setup.sh deploy standard [--profile <MODAL_PROFILE>]
  scripts/setup.sh deploy split [--profile <MODAL_PROFILE>]
  scripts/setup.sh deploy worker

prepare: 依存関係を導入し、未作成の場合だけ .env を用意します。
deploy:  ログイン・設定済みのアカウントへ、指定した対象を反映します。
手順: docs/guides/agent-setup.md
EOF
}

action="${1:-}"
target="${2:-}"
profile=""
case "$action" in
  -h | --help)
    usage
    exit 0
    ;;
  prepare)
    if [ "$#" -ne 1 ]; then
      usage
      exit 2
    fi
    ;;
  deploy)
    case "$target" in
      standard | split)
        if [ "$#" -eq 4 ] && [ "$3" = "--profile" ] && [ -n "$4" ]; then
          profile="$4"
        elif [ "$#" -ne 2 ]; then
          usage
          exit 2
        fi
        ;;
      worker)
        if [ "$#" -ne 2 ]; then
          usage
          exit 2
        fi
        ;;
      *)
        usage
        exit 2
        ;;
    esac
    ;;
  *)
    usage
    exit 2
    ;;
esac

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'error: %s が必要です。docs/guides/agent-setup.md の環境準備を確認してください。\n' "$1" >&2
    exit 1
  fi
}

require_node() {
  require_command node
  require_command npm
  local expected actual
  expected="$(cat .node-version)"
  actual="$(node --version)"
  if [ "$actual" != "v$expected" ]; then
    printf 'error: Node.js %s が必要です（現在: %s）。.node-version に合わせて切り替えてください。\n' \
      "$expected" "$actual" >&2
    exit 1
  fi
}

trap 'printf "error: 処理を中断しました。docs/guides/agent-setup.md で準備・ログイン・設定を確認してください。\n" >&2' ERR

if [ "$action" = "prepare" ]; then
  require_command uv
  require_node
  uv sync --locked --extra split-test
  if [ ! -e .env ]; then
    cp .env.example .env
  fi
  cd worker
  npm ci
  printf '環境準備が完了しました。ガイドに従ってログイン・接続先の確認へ進んでください。\n'
elif [ "$target" = "worker" ]; then
  require_node
  if [ ! -d worker/node_modules ]; then
    printf 'error: 先に ./scripts/setup.sh prepare を実行してください。\n' >&2
    exit 1
  fi
  cd worker
  npm test
  npm run typecheck
  npm run deploy
else
  require_command uv
  if [ "$target" = "standard" ]; then
    app="comfyapp.py"
    export COMFYUI_REQUIRES_PROXY_AUTH=on
  else
    app="splitapp.py"
  fi
  if [ -n "$profile" ]; then
    exec ./scripts/modal.sh --profile "$profile" deploy "$app"
  fi
  exec ./scripts/modal.sh deploy "$app"
fi
