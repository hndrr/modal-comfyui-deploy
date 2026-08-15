#!/usr/bin/env bash
# Run the Modal CLI against a specific profile (= a specific Modal account).
#
#   scripts/modal.sh --list
#   scripts/modal.sh <profile> deploy comfyapp.py
#   scripts/modal.sh <profile> volume ls comfy-model /
#
# Volumes, Secrets and proxy auth tokens are per workspace, so hitting the wrong
# account silently shows an empty volume (or uploads to the wrong place). This
# wrapper refuses to run for a profile that is not in the Modal config file.
#
# The active profile in ~/.modal.toml is left alone; see docs/modal-profiles.md.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/modal.sh --list                     Show the configured profiles
  scripts/modal.sh <profile> <modal args...>  Run the Modal CLI as <profile>

Examples:
  scripts/modal.sh <profile> profile current
  scripts/modal.sh <profile> deploy comfyapp.py
EOF
}

if [ "$#" -eq 0 ]; then
  usage
  exit 1
fi

case "$1" in
  --list | -l)
    exec uv run modal profile list
    ;;
  -h | --help)
    usage
    exit 0
    ;;
esac

profile="$1"
shift

if [ "$#" -eq 0 ]; then
  echo "error: no Modal command given after the profile name." >&2
  usage
  exit 1
fi

config_path="${MODAL_CONFIG_PATH:-$HOME/.modal.toml}"
if [ ! -f "$config_path" ]; then
  echo "error: Modal config not found at $config_path." >&2
  echo "Log in first: uv run modal token new" >&2
  exit 1
fi

if ! grep -qF "[$profile]" "$config_path"; then
  echo "error: profile '$profile' is not in $config_path." >&2
  echo "Configured profiles:" >&2
  uv run modal profile list >&2
  exit 1
fi

echo "modal: using profile '$profile'" >&2
MODAL_PROFILE="$profile" exec uv run modal "$@"
