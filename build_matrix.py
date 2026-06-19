#!/usr/bin/env python3
"""Build the GitHub Actions matrix of countries to verify.

Country universe = every country that has channels in the iptv-org database
(so a `countries/<cc>.m3u` playlist exists). For each country, mode is:
  - "vpn"    if VPNGate has a server there  -> verify behind a same-country VPN
  - "direct" otherwise                      -> verify from the US runner (geo-limited)

Output: compact JSON {"include": [{"country": "id", "mode": "vpn"}, ...]} for
`strategy.matrix: ${{ fromJSON(...) }}`. GitHub caps a matrix at 256 jobs; if
there are more countries we keep VPN-capable ones first and log what was dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
MATRIX_CAP = 256  # GitHub Actions hard limit on matrix job count.


def fetch_channels() -> list[dict]:
    req = urllib.request.Request(
        CHANNELS_URL, headers={"User-Agent": "iptv-verify/1.0"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iptv_countries(channels: list[dict]) -> set[str]:
    codes: set[str] = set()
    for ch in channels:
        cc = (ch.get("country") or "").strip().lower()
        if cc:
            codes.add(cc)
    return codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the verify matrix JSON.")
    parser.add_argument(
        "--vpn", required=True, help="JSON file {cc: count} of VPNGate countries."
    )
    parser.add_argument("--out", help="Write matrix JSON here (default: stdout).")
    parser.add_argument(
        "--channels", help="Cached channels.json (default: fetch from iptv-org)."
    )
    args = parser.parse_args(argv)

    if args.channels:
        with open(args.channels, "r", encoding="utf-8") as fh:
            channels = json.load(fh)
    else:
        channels = fetch_channels()

    with open(args.vpn, "r", encoding="utf-8") as fh:
        vpn_countries = set(json.load(fh).keys())

    countries = iptv_countries(channels)
    # VPN-capable countries first so they survive the 256 cap; then alpha order.
    ordered = sorted(countries, key=lambda c: (c not in vpn_countries, c))

    dropped = ordered[MATRIX_CAP:]
    kept = ordered[:MATRIX_CAP]
    if dropped:
        print(
            f"warning: {len(countries)} countries > {MATRIX_CAP} cap; "
            f"dropped {len(dropped)} direct-only: {','.join(dropped)}",
            file=sys.stderr,
        )

    include = [
        {"country": cc, "mode": "vpn" if cc in vpn_countries else "direct"}
        for cc in kept
    ]
    matrix = json.dumps({"include": include})

    vpn_n = sum(1 for x in include if x["mode"] == "vpn")
    print(
        f"matrix: {len(include)} countries ({vpn_n} vpn, {len(include) - vpn_n} direct)",
        file=sys.stderr,
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(matrix)
    else:
        print(matrix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
