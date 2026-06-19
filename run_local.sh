#!/usr/bin/env bash
# Verify a playlist locally and write the result, before pushing to GitHub.
#
# No arguments: verifies the full iptv-org playlist into index.m3u using your
# own IP, so channels in your own country verify without any VPN.
#
# Any arguments are passed straight through to verify.py, e.g. a quick
# single-country test:
#   ./run_local.sh --url https://iptv-org.github.io/iptv/countries/id.m3u --output id.m3u --workers 60
set -euo pipefail

cd "$(dirname "$0")"

# Override if your default python3 is not the one you want (needs 3.11+):
#   PYTHON=python3.12 ./run_local.sh
PYTHON="${PYTHON:-python3}"
DEFAULT_URL="https://iptv-org.github.io/iptv/index.m3u"

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "error: ffprobe not found. Install ffmpeg first:" >&2
  echo "  macOS:  brew install ffmpeg" >&2
  echo "  Debian: sudo apt-get install ffmpeg" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "creating .venv" >&2
  # Some Python builds ship a broken ensurepip; deps are stdlib-only, so fall
  # back to a pip-less venv rather than failing.
  "$PYTHON" -m venv .venv 2>/dev/null \
    || { rm -rf .venv; "$PYTHON" -m venv --without-pip .venv; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate
# Install deps only if this venv actually has pip (stdlib-only needs none).
if python -m pip --version >/dev/null 2>&1; then
  python -m pip install -q -r requirements.txt || true
fi

if [ "$#" -gt 0 ]; then
  python verify.py "$@"
else
  mkdir -p m3u
  python verify.py --url "$DEFAULT_URL" --output m3u/index.m3u --workers 40 --timeout 8
fi
