#!/usr/bin/env bash
# Full local build: verify EVERY iptv-org country, each behind a same-country
# VPNGate VPN where a relay exists (direct from your own IP otherwise), then
# merge everything into m3u/index.m3u. This mirrors the GitHub Actions pipeline
# but runs serially on your machine, so it can take a long time (hours for the
# full country set). Use it as a manual fallback when CI is not available.
#
# Layout it writes:
#   ovpn/        VPNGate cache + per-country .ovpn (gitignored)
#   m3u/countries/<cc>.m3u   per-country playable results (tracked)
#   m3u/index.m3u            merged, de-duplicated playlist (tracked)
#
# Needs: ffprobe (ffmpeg), openvpn, sudo (OpenVPN sets up the tunnel).
# Note: on macOS OpenVPN routing can need extra setup; any country whose VPN
# fails is verified direct instead, so the build still completes.
set -uo pipefail # not -e: a single bad country must not abort the whole run

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"

WORKERS_VPN="${WORKERS_VPN:-25}"
TIMEOUT_VPN="${TIMEOUT_VPN:-15}"
WORKERS_DIRECT="${WORKERS_DIRECT:-50}"
TIMEOUT_DIRECT="${TIMEOUT_DIRECT:-8}"
BASE="https://iptv-org.github.io/iptv/countries"

command -v ffprobe >/dev/null || { echo "error: need ffmpeg/ffprobe" >&2; exit 1; }
command -v openvpn >/dev/null || { echo "error: need openvpn (brew install openvpn)" >&2; exit 1; }

mkdir -p ovpn m3u/countries

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv 2>/dev/null || { rm -rf .venv; "$PYTHON" -m venv --without-pip .venv; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Keep sudo warm for the whole run, and always tear the tunnel down on exit.
sudo -v
( while true; do sudo -n true; sleep 60; done ) 2>/dev/null &
KEEPALIVE=$!
trap 'sudo pkill openvpn 2>/dev/null; kill "$KEEPALIVE" 2>/dev/null' EXIT

current_country() {
  curl -s --max-time 10 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]' | tr 'A-Z' 'a-z'
}

connect_vpn() {
  local want="$1" rank ovpn i
  for rank in 0 1 2 3 4; do
    ovpn="ovpn/${want}.ovpn"
    python vpngate.py get-config "$want" --rank "$rank" --csv ovpn/vpngate.csv --out "$ovpn" 2>/dev/null || return 1
    sudo openvpn --config "$ovpn" --daemon --log "ovpn/${want}.log"
    for i in $(seq 1 12); do
      sleep 3
      [ "$(current_country)" = "$want" ] && return 0
    done
    sudo pkill openvpn 2>/dev/null
    sleep 2
  done
  return 1
}

verify_country() {
  local cc="$1" workers="$2" timeout="$3"
  python verify.py --url "$BASE/${cc}.m3u" --output "m3u/countries/${cc}.m3u" \
    --workers "$workers" --timeout "$timeout" --skip-empty || true
}

echo "fetching VPNGate list..." >&2
python vpngate.py fetch --out ovpn/vpngate.csv
python vpngate.py list-countries --csv ovpn/vpngate.csv --out ovpn/vpn_countries.json
python build_matrix.py --vpn ovpn/vpn_countries.json --out ovpn/matrix.json

# Iterate "cc mode" lines from the matrix.
while read -r cc mode; do
  [ -z "$cc" ] && continue
  if [ "$mode" = "vpn" ]; then
    if connect_vpn "$cc"; then
      echo ">> $cc : vpn" >&2
      verify_country "$cc" "$WORKERS_VPN" "$TIMEOUT_VPN"
      sudo pkill openvpn 2>/dev/null
      sleep 2
    else
      echo ">> $cc : vpn failed, direct" >&2
      verify_country "$cc" "$WORKERS_DIRECT" "$TIMEOUT_DIRECT"
    fi
  else
    echo ">> $cc : direct" >&2
    verify_country "$cc" "$WORKERS_DIRECT" "$TIMEOUT_DIRECT"
  fi
done < <(python -c "import json; [print(x['country'], x['mode']) for x in json.load(open('ovpn/matrix.json'))['include']]")

echo "merging..." >&2
MERGE_ARGS=()
[ -n "${SITE_URL:-}" ] && MERGE_ARGS+=(--site-url "$SITE_URL")
[ -n "${REPO_URL:-}" ] && MERGE_ARGS+=(--repo-url "$REPO_URL")
python merge.py --new-dir m3u/countries --site-dir .localstate --output m3u/index.m3u "${MERGE_ARGS[@]}"

echo "done -> m3u/index.m3u (+ index.html)" >&2
