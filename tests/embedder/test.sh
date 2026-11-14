#!/usr/bin/env bash
# Runs the test embedder that build.py made, on a machine like its target's: test.sh <embedder tar>. A Linux machine
# gets the system libraries Chromium loads at runtime first (debian-packages.txt).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
directory="$(mktemp -d)"
tar -xf "$1" -C "$directory"
if [ "$(uname -s)" = Linux ]; then
  mapfile -t packages < <(grep -v '^#' "$here/debian-packages.txt" | grep .)
  sudo -n apt-get update -q
  sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -yq --no-install-recommends "${packages[@]}"
fi
python3 "$here/run.py" "$directory"
