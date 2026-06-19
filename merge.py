#!/usr/bin/env python3
"""Merge per-country playlists into one index.m3u, with stale-result guards.

Inputs:
  --new-dir   Fresh `<cc>.m3u` results from this run (matrix artifacts).
  --site-dir  Checked-out gh-pages (previous publish). Holds the previous
              index.m3u and state/ (per-country playlists + counts.json).

Guards (a flaky run / dead VPN must not wipe a good playlist):
  Per-country: if a country's new playable count collapses (below an absolute
               floor, or dropped more than --country-drop vs last run), reuse
               that country's PREVIOUS playlist instead of the new one.
  Global:      if the assembled total collapses (below --min-total, or dropped
               more than --total-drop vs last run), keep the PREVIOUS index.m3u
               and state untouched and publish nothing new.

On success the site-dir is rewritten in place (index.m3u + state/) for the
deploy step to publish.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from string import Template

from verify import Channel, parse_m3u

COUNTRIES_URL = "https://iptv-org.github.io/api/countries.json"
GROUP_TITLE_RE = re.compile(r'group-title="([^"]*)"')

STATE_DIR = "state"
COUNTS_FILE = "counts.json"
COUNTRIES_DIR = "countries"


def read_channels(path: str) -> list[Channel]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        _, channels = parse_m3u(fh.read())
    return channels


def write_channels(path: str, channels: list[Channel], header: str = "#EXTM3U") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for ch in channels:
            fh.write(ch.render() + "\n")


def load_country_names(path: str | None) -> dict[str, str]:
    """Map ISO alpha-2 code (lowercase) to country name, e.g. id -> Indonesia.

    Reads a cached countries.json if given, else fetches it from iptv-org. On
    any failure returns an empty map; callers fall back to the bare code.
    """
    try:
        if path:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            req = urllib.request.Request(
                COUNTRIES_URL, headers={"User-Agent": "iptv-verify/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network/parse - degrade to bare codes
        print(f"warning: country names unavailable ({exc})", file=sys.stderr)
        return {}
    return {
        c["code"].lower(): c["name"]
        for c in data
        if c.get("code") and c.get("name")
    }


def relabel_country(channel: Channel, label: str) -> Channel:
    """Return a copy whose EXTINF group-title is prefixed with the country.

    "News" -> "Indonesia / News"; a missing or "Undefined" category becomes
    just "Indonesia". Only the index entry is relabeled; stored state keeps the
    original tags so re-merges stay idempotent.
    """
    new_tags: list[str] = []
    for tag in channel.tags:
        if not tag.startswith("#EXTINF"):
            new_tags.append(tag)
            continue
        m = GROUP_TITLE_RE.search(tag)
        old = m.group(1).strip() if m else ""
        value = f"{label} / {old}" if old and old.lower() != "undefined" else label
        if m:
            tag = GROUP_TITLE_RE.sub(f'group-title="{value}"', tag, count=1)
        else:
            head, sep, name = _split_extinf(tag)
            tag = f'{head} group-title="{value}"{sep}{name}'
        new_tags.append(tag)
    return Channel(tags=new_tags, url=channel.url)


def _split_extinf(line: str) -> tuple[str, str, str]:
    """Split an EXTINF line at the comma before the display name (quote-aware)."""
    in_quote = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            return line[:i], ",", line[i + 1 :]
    return line, "", ""


PAGE_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<meta name="description" content="$desc">
<meta name="robots" content="index, follow">
$canonical<meta property="og:type" content="website">
<meta property="og:title" content="$title">
<meta property="og:description" content="$desc">
$og_url<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="$title">
<meta name="twitter:description" content="$desc">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Dataset","name":"$title","description":"$desc","dateModified":"$iso","creator":{"@type":"Organization","name":"iptv-org","url":"https://github.com/iptv-org/iptv"}$json_url}
</script>
<style>
:root{color-scheme:light dark;--bg:#fafafa;--fg:#16181d;--muted:#6b7280;--card:#fff;--line:#e6e8eb;--accent:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#0d0f13;--fg:#e8eaed;--muted:#9aa0a6;--card:#15181e;--line:#262a31;--accent:#6ea8fe}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:2rem;
font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
main{width:100%;max-width:720px}
h1{font-size:1.5rem;margin:0 0 .25rem;letter-spacing:-.01em}
.lead{color:var(--muted);margin:0 0 2rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.25rem 1.5rem;margin-bottom:1.5rem}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:.5rem}
.urlrow{display:flex;gap:.5rem;align-items:stretch}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.9rem;
white-space:nowrap;overflow-x:auto;
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:.6rem .75rem;flex:1;display:flex;align-items:center}
button{font:inherit;font-size:.85rem;cursor:pointer;border:1px solid var(--line);background:var(--accent);color:#fff;
border-radius:8px;padding:0 1rem;white-space:nowrap;min-width:5.5rem;transition:background .15s}
button:active{transform:translateY(1px)}
button.ok{background:#16a34a;border-color:#16a34a}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);
border-radius:12px;overflow:hidden;margin-bottom:1.5rem}
.stat{background:var(--card);padding:1.1rem 1rem;text-align:center}
.stat b{display:block;font-size:1.6rem;letter-spacing:-.02em}
.stat span{font-size:.78rem;color:var(--muted)}
.how{color:var(--muted);font-size:.92rem}
footer{color:var(--muted);font-size:.82rem;margin-top:2rem}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
</style>
</head>
<body>
<main>
<h1>$title</h1>
<p class="lead">$desc</p>
<div class="card">
<div class="label">Playlist URL</div>
<div class="urlrow">
<code id="u">$url</code>
<button id="c" type="button">Copy</button>
</div>
</div>
<div class="stats">
<div class="stat"><b>$channels</b><span>channels</span></div>
<div class="stat"><b>$countries</b><span>countries</span></div>
<div class="stat"><b>$updated</b><span>updated (UTC)</span></div>
</div>
<p class="how">Paste the URL into any M3U player (VLC, TiViMate, Kodi, IPTV Smarters).
Channels are grouped by country. Dead and unreachable streams are removed, and
the list is rebuilt automatically about every 6 hours, so keep the same URL and
your player picks up each refresh.</p>
<footer>Source links from <a href="https://github.com/iptv-org/iptv">iptv-org</a>.
This site stores no video, only checks which public links still play.$repo</footer>
</main>
<script>
(function(){
var b=document.getElementById('c'),u=document.getElementById('u'),t;
b.addEventListener('click',function(){
navigator.clipboard.writeText(u.textContent).then(function(){
b.textContent='Copied';b.classList.add('ok');
clearTimeout(t);t=setTimeout(function(){b.textContent='Copy';b.classList.remove('ok');},1500);
});
});
})();
</script>
</body>
</html>
"""
)


def build_html(
    *, total: int, countries: int, updated: str, iso: str, site_url: str | None, repo_url: str | None
) -> str:
    title = "iptv-curated"
    desc = (
        "Auto-verified IPTV playlist: only channels that actually play, grouped "
        "by country and rebuilt automatically. Free M3U for VLC, TiViMate, Kodi."
    )
    playlist = f"{site_url}/index.m3u" if site_url else "index.m3u"
    canonical = f'<link rel="canonical" href="{html.escape(site_url)}/">\n' if site_url else ""
    og_url = f'<meta property="og:url" content="{html.escape(site_url)}/">\n' if site_url else ""
    json_url = f',"url":"{site_url}/"' if site_url else ""
    repo = (
        f' <a href="{html.escape(repo_url)}">Source on GitHub</a>.' if repo_url else ""
    )
    return PAGE_TEMPLATE.substitute(
        title=title,
        desc=desc,
        url=html.escape(playlist),
        canonical=canonical,
        og_url=og_url,
        json_url=json_url,
        iso=iso,
        channels=f"{total:,}",
        countries=str(countries),
        updated=updated,
        repo=repo,
    )


def country_codes(*dirs: str) -> list[str]:
    codes: set[str] = set()
    for d in dirs:
        if d and os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".m3u"):
                    codes.add(name[:-4].lower())
    return sorted(codes)


def keep_new(new_count: int, prev_count: int, min_country: int, drop: float) -> bool:
    """True = use this run's result; False = reuse the previous (it regressed).

    Reuse previous only when it is strictly larger AND the new result either
    fell below the floor or collapsed by more than `drop` vs last run.
    """
    if prev_count > new_count and (
        new_count < min_country or new_count < prev_count * (1 - drop)
    ):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge per-country playlists.")
    parser.add_argument("--new-dir", required=True, help="Fresh <cc>.m3u results.")
    parser.add_argument("--site-dir", required=True, help="Previous publish dir.")
    parser.add_argument("--output", required=True, help="index.m3u path to write.")
    parser.add_argument("--min-country", type=int, default=3)
    parser.add_argument("--country-drop", type=float, default=0.5)
    parser.add_argument("--min-total", type=int, default=200)
    parser.add_argument("--total-drop", type=float, default=0.5)
    parser.add_argument(
        "--country-names",
        help="Cached countries.json (default: fetch from iptv-org) for group labels.",
    )
    parser.add_argument(
        "--no-country-groups",
        action="store_true",
        help="Keep original group-title; do not relabel by country.",
    )
    parser.add_argument(
        "--site-url",
        help="Public base URL (e.g. https://user.github.io/repo) for the landing "
        "page links and SEO tags.",
    )
    parser.add_argument(
        "--repo-url", help="GitHub repo URL, shown as a link on the landing page."
    )
    parser.add_argument(
        "--no-html", action="store_true", help="Do not write the index.html landing page."
    )
    args = parser.parse_args(argv)

    state_dir = os.path.join(args.site_dir, STATE_DIR)
    prev_countries_dir = os.path.join(state_dir, COUNTRIES_DIR)
    counts_path = os.path.join(state_dir, COUNTS_FILE)

    prev_counts: dict[str, int] = {}
    if os.path.isfile(counts_path):
        with open(counts_path, "r", encoding="utf-8") as fh:
            prev_counts = json.load(fh)

    chosen: dict[str, list[Channel]] = {}
    new_counts: dict[str, int] = {}

    for cc in country_codes(args.new_dir, prev_countries_dir):
        new_ch = read_channels(os.path.join(args.new_dir, f"{cc}.m3u"))
        prev_ch = read_channels(os.path.join(prev_countries_dir, f"{cc}.m3u"))
        prev_n = prev_counts.get(cc, len(prev_ch))

        if keep_new(len(new_ch), prev_n, args.min_country, args.country_drop):
            chosen[cc] = new_ch
        else:
            chosen[cc] = prev_ch
            print(
                f"guard: {cc} new={len(new_ch)} prev={prev_n} -> reuse previous",
                file=sys.stderr,
            )
        new_counts[cc] = len(chosen[cc])

    # Assemble the global index, de-duplicating by stream URL and relabeling
    # each channel's group-title with its country so players group by country.
    names = {} if args.no_country_groups else load_country_names(args.country_names)
    seen: set[str] = set()
    index: list[Channel] = []
    for cc in sorted(chosen):
        label = names.get(cc, cc.upper())
        for ch in chosen[cc]:
            if ch.url not in seen:
                seen.add(ch.url)
                index.append(ch if args.no_country_groups else relabel_country(ch, label))

    total = len(index)
    prev_total = sum(prev_counts.values())

    # Global guard: refuse to publish a collapsed playlist over a good one.
    if prev_total > 0 and (total < args.min_total or total < prev_total * (1 - args.total_drop)):
        print(
            f"GLOBAL GUARD TRIPPED: total={total} prev={prev_total} "
            f"(min={args.min_total}, drop>{args.total_drop:.0%}); keeping previous publish",
            file=sys.stderr,
        )
        return 0  # leave site-dir untouched; deploy republishes the old files

    write_channels(args.output, index)
    for cc, channels in chosen.items():
        write_channels(os.path.join(prev_countries_dir, f"{cc}.m3u"), channels)
    os.makedirs(state_dir, exist_ok=True)
    with open(counts_path, "w", encoding="utf-8") as fh:
        json.dump(new_counts, fh, sort_keys=True)

    if not args.no_html:
        now = datetime.now(timezone.utc)
        countries_published = sum(1 for n in new_counts.values() if n > 0)
        page = build_html(
            total=total,
            countries=countries_published,
            updated=now.strftime("%Y-%m-%d %H:%M"),
            iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            site_url=args.site_url.rstrip("/") if args.site_url else None,
            repo_url=args.repo_url,
        )
        html_path = os.path.join(os.path.dirname(args.output) or ".", "index.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(page)

    print(
        f"published {total} channels across {len(chosen)} countries "
        f"(prev total {prev_total})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
