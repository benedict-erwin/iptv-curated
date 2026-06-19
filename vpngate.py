#!/usr/bin/env python3
"""VPNGate helper: fetch the public server list and emit OpenVPN configs.

Subcommands:
  fetch           Download the VPNGate CSV once (cache it as an artifact).
  list-countries  Emit JSON {country_code: server_count} for countries that
                  have at least one usable OpenVPN server.
  get-config      Decode the OpenVPN config for a country's Nth-best server.

Network is only touched when --csv is not given. In CI, `fetch` once in the
plan job, share the CSV as an artifact, then pass --csv to the other commands
so the VPNGate servers aren't hammered once per matrix job.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import sys
import urllib.request

# Public mirrors of the VPNGate API (CSV with embedded base64 OpenVPN configs).
VPNGATE_URLS = [
    "https://www.vpngate.net/api/iphone/",
    "http://www.vpngate.net/api/iphone/",
]

CONFIG_COL = "OpenVPN_ConfigData_Base64"
COUNTRY_COL = "CountryShort"
SCORE_COL = "Score"


def fetch_csv() -> str:
    last_err: Exception | None = None
    for url in VPNGATE_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "iptv-verify/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # network/HTTP - try the next mirror
            last_err = exc
    raise RuntimeError(f"could not fetch VPNGate list: {last_err}")


def parse_rows(text: str) -> list[dict[str, str]]:
    """Parse the VPNGate CSV into a list of row dicts.

    The payload looks like:
        *vpn
        #HostName,IP,Score,...,OpenVPN_ConfigData_Base64
        host,ip,...,<base64>
        ...
        *
    """
    header: list[str] | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("#HostName"):
            header = next(csv.reader([line[1:]]))  # drop the leading '#'
            continue
        if line.startswith("*"):
            continue
        if header is not None:
            data_lines.append(line)

    if header is None:
        return []

    rows: list[dict[str, str]] = []
    for fields in csv.reader(io.StringIO("\n".join(data_lines))):
        if len(fields) != len(header):
            continue
        rows.append(dict(zip(header, fields)))
    return rows


def usable_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get(CONFIG_COL) and r.get(COUNTRY_COL)]


def _score(row: dict[str, str]) -> int:
    try:
        return int(row.get(SCORE_COL, "0"))
    except ValueError:
        return 0


def servers_for(rows: list[dict[str, str]], country: str) -> list[dict[str, str]]:
    cc = country.lower()
    matches = [r for r in usable_rows(rows) if r[COUNTRY_COL].lower() == cc]
    matches.sort(key=_score, reverse=True)
    return matches


def load_rows(csv_path: str | None) -> list[dict[str, str]]:
    if csv_path:
        with open(csv_path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_rows(fh.read())
    return parse_rows(fetch_csv())


def cmd_fetch(args: argparse.Namespace) -> int:
    text = fetch_csv()
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    rows = usable_rows(parse_rows(text))
    print(f"saved {len(rows)} usable servers to {args.out}", file=sys.stderr)
    return 0


def cmd_list_countries(args: argparse.Namespace) -> int:
    rows = usable_rows(load_rows(args.csv))
    counts: dict[str, int] = {}
    for r in rows:
        cc = r[COUNTRY_COL].lower()
        counts[cc] = counts.get(cc, 0) + 1
    out = json.dumps(counts, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
    else:
        print(out)
    print(f"{len(counts)} countries with servers", file=sys.stderr)
    return 0


def cmd_get_config(args: argparse.Namespace) -> int:
    rows = load_rows(args.csv)
    candidates = servers_for(rows, args.country)
    if args.rank >= len(candidates):
        print(
            f"error: no rank {args.rank} server for {args.country} "
            f"(have {len(candidates)})",
            file=sys.stderr,
        )
        return 1
    row = candidates[args.rank]
    config = base64.b64decode(row[CONFIG_COL]).decode("utf-8", errors="replace")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(config)
    else:
        sys.stdout.write(config)
    print(
        f"{args.country} rank {args.rank}: {row.get('HostName')} "
        f"({row.get('IP')}) score={_score(row)}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VPNGate server-list helper.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Download the VPNGate CSV.")
    p_fetch.add_argument("--out", default="vpngate.csv")
    p_fetch.set_defaults(func=cmd_fetch)

    p_list = sub.add_parser("list-countries", help="Emit {cc: server_count} JSON.")
    p_list.add_argument("--csv", help="Read a cached CSV instead of fetching.")
    p_list.add_argument("--out", help="Write JSON here (default: stdout).")
    p_list.set_defaults(func=cmd_list_countries)

    p_cfg = sub.add_parser("get-config", help="Emit an OpenVPN config for a country.")
    p_cfg.add_argument("country", help="ISO 3166 alpha-2 code (e.g. id, us, jp).")
    p_cfg.add_argument("--rank", type=int, default=0, help="0 = best server.")
    p_cfg.add_argument("--csv", help="Read a cached CSV instead of fetching.")
    p_cfg.add_argument("--out", help="Write the .ovpn here (default: stdout).")
    p_cfg.set_defaults(func=cmd_get_config)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
