#!/bin/bash
# Installs Sweater Kit as an Omarchy shell plugin (service + bar toggle).
# For Omarchy without the plugin system, see "Without the plugin system" in README.md.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
target="$HOME/.config/omarchy/plugins/mickul.sweater-kit"

mkdir -p "$(dirname "$target")"
rm -rf "$target"
rsync -a --exclude .git --exclude __pycache__ "$here/" "$target/"
omarchy plugin validate "$target"
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
sleep 1
omarchy plugin enable mickul.sweater-kit "$@"
echo "installed $target"
