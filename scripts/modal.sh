#!/usr/bin/env bash
# Run the Modal CLI against the account this checkout is pinned to.
#
#   scripts/modal.sh use <profile>       Pin this checkout to a profile
#   scripts/modal.sh use --clear         Unpin (fall back to the active profile)
#   scripts/modal.sh deploy comfyapp.py  Run the Modal CLI with the pin applied
#   scripts/modal.sh --profile <p> ...   Override the pin for one command
#   scripts/modal.sh --list              Show profiles and the current pin
#
# The pin lives in .modal-profile (gitignored) so it is per checkout, and the
# global active profile in ~/.modal.toml is never touched. Precedence matches
# the asset manager: MODAL_PROFILE > .modal-profile > active profile.
#
# See docs/modal-profiles.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_FILE="$REPO_ROOT/.modal-profile"
CONFIG_PATH="${MODAL_CONFIG_PATH:-$HOME/.modal.toml}"

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/modal.sh use <profile>          このリポジトリで使うプロファイルを固定
  scripts/modal.sh use --clear            固定を解除（~/.modal.toml の既定に戻す）
  scripts/modal.sh --list                 プロファイル一覧と現在の固定先
  scripts/modal.sh <modal args...>        固定先で Modal CLI を実行
  scripts/modal.sh --profile <p> <args>   1 コマンドだけ別プロファイルで実行

Examples:
  scripts/modal.sh use work
  scripts/modal.sh deploy comfyapp.py
  scripts/modal.sh volume ls comfy-model /
EOF
}

read_pin() {
  [ -f "$PROFILE_FILE" ] || return 0
  grep -v '^[[:space:]]*#' "$PROFILE_FILE" | sed -n '/[^[:space:]]/{s/^[[:space:]]*//;s/[[:space:]]*$//;p;q;}'
}

require_known_profile() {
  local profile="$1"
  if [ ! -f "$CONFIG_PATH" ]; then
    echo "error: Modal config not found at $CONFIG_PATH." >&2
    echo "Log in first: uv run modal token new" >&2
    exit 1
  fi
  if ! grep -qF "[$profile]" "$CONFIG_PATH"; then
    echo "error: profile '$profile' is not in $CONFIG_PATH." >&2
    echo "Configured profiles:" >&2
    uv run modal profile list >&2
    exit 1
  fi
}

if [ "$#" -eq 0 ]; then
  usage
  exit 1
fi

case "$1" in
  -h | --help)
    usage
    exit 0
    ;;
  use)
    shift
    if [ "$#" -ne 1 ]; then
      echo "error: 'use' takes exactly one profile name (or --clear)." >&2
      usage
      exit 1
    fi
    if [ "$1" = "--clear" ]; then
      rm -f "$PROFILE_FILE"
      echo "unpinned: ~/.modal.toml の既定プロファイルを使います" >&2
      exit 0
    fi
    require_known_profile "$1"
    printf '# このリポジトリで使う Modal プロファイル（docs/modal-profiles.md）\n%s\n' "$1" \
      >"$PROFILE_FILE"
    echo "pinned: $1" >&2
    exit 0
    ;;
  --list | -l)
    pin="$(read_pin)"
    echo "pinned profile: ${pin:-(なし: ~/.modal.toml の既定を使用)}" >&2
    exec uv run modal profile list
    ;;
  --profile)
    shift
    if [ "$#" -lt 2 ]; then
      echo "error: --profile needs a profile name and a Modal command." >&2
      usage
      exit 1
    fi
    profile="$1"
    shift
    require_known_profile "$profile"
    echo "modal: using profile '$profile' (--profile)" >&2
    MODAL_PROFILE="$profile" exec uv run modal "$@"
    ;;
esac

# No subcommand of ours: run the Modal CLI with the pin applied.
if [ -n "${MODAL_PROFILE:-}" ]; then
  echo "modal: using profile '$MODAL_PROFILE' (MODAL_PROFILE)" >&2
  exec uv run modal "$@"
fi

pin="$(read_pin)"
if [ -n "$pin" ]; then
  require_known_profile "$pin"
  echo "modal: using profile '$pin' (.modal-profile)" >&2
  MODAL_PROFILE="$pin" exec uv run modal "$@"
fi

echo "modal: using the active profile in $CONFIG_PATH" >&2
exec uv run modal "$@"
