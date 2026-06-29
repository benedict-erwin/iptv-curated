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
import unicodedata
import urllib.request
from datetime import datetime, timezone
from string import Template
from urllib.parse import quote

from verify import Channel, parse_m3u

COUNTRIES_URL = "https://iptv-org.github.io/api/countries.json"
GROUP_TITLE_RE = re.compile(r'group-title="([^"]*)"')
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

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


def parse_extinf(tags: list[str]) -> tuple[dict[str, str], str]:
    """Return (attributes, display name) from a channel's EXTINF line."""
    for tag in tags:
        if tag.startswith("#EXTINF"):
            head, _, name = _split_extinf(tag)
            return dict(ATTR_RE.findall(head)), name.strip()
    return {}, ""


def channel_record(ch: Channel, cc: str, cn: str) -> dict[str, str]:
    """Flatten one channel into the app's short-key record."""
    attrs, title = parse_extinf(ch.tags)
    category = (attrs.get("group-title", "") or "").strip()
    if not category or category.lower() == "undefined":
        category = "Other"
    return {
        "n": title or attrs.get("tvg-id", "") or "Unknown",
        "u": ch.url,
        "l": attrs.get("tvg-logo", ""),
        "i": attrs.get("tvg-id", ""),
        "cc": cc,
        "cn": cn,
        "g": category,
    }


def write_data_json(
    path: str,
    chosen: dict[str, list[Channel]],
    names: dict[str, str],
    updated_iso: str,
    top_picks: list[dict] | None = None,
    discover: list[dict] | None = None,
) -> None:
    """Write the flat channel dataset the browser app filters over.

    Short keys keep the file small: n=name, u=url, l=logo, i=tvg-id,
    cc=country code, cn=country name, g=category.
    """
    channels: list[dict[str, str]] = []
    for cc in sorted(chosen):
        cn = names.get(cc, cc.upper())
        for ch in chosen[cc]:
            if ch.url:
                channels.append(channel_record(ch, cc, cn))
    payload = {
        "updated": updated_iso,
        "count": len(channels),
        "top_picks": top_picks or [],
        "discover": discover or [],
        "channels": channels,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


def load_seed(path: str) -> list[dict[str, str]]:
    """Load the curated recommendation seed (tvg-id + name, in priority order)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict)]


def set_group_title(channel: Channel, value: str) -> Channel:
    """Return a copy whose EXTINF group-title is replaced with a fixed value."""
    new_tags: list[str] = []
    for tag in channel.tags:
        if tag.startswith("#EXTINF"):
            if GROUP_TITLE_RE.search(tag):
                tag = GROUP_TITLE_RE.sub(f'group-title="{value}"', tag, count=1)
            else:
                head, sep, name = _split_extinf(tag)
                tag = f'{head} group-title="{value}"{sep}{name}'
        new_tags.append(tag)
    return Channel(tags=new_tags, url=channel.url)


def build_top_picks(
    chosen: dict[str, list[Channel]],
    seed: list[dict[str, str]],
    names: dict[str, str],
    per_category: int,
) -> list[dict]:
    """Pick up to `per_category` playable channels per seed category.

    Seed entries carry a "cat" bucket (News, Sports, ...). For each bucket, in
    its priority order, keep the first playable matches (by tvg-id, then name),
    skipping geo-blocked streams. Returns one group per category, in the order
    the categories first appear in the seed:
        [{"cat": "News", "items": [{"ch": Channel, "d": record}, ...]}, ...]
    """
    by_id: dict[str, tuple] = {}
    by_name: dict[str, tuple] = {}
    for cc in sorted(chosen):
        cn = names.get(cc, cc.upper())
        for ch in chosen[cc]:
            if not ch.url:
                continue
            attrs, title = parse_extinf(ch.tags)
            if "[geo-blocked]" in title.lower():
                continue  # never recommend a geo-blocked stream
            rec = (ch, cc, cn)
            tid = (attrs.get("tvg-id", "") or "").lower()
            if tid and tid not in by_id:
                by_id[tid] = rec
            nm = title.lower()
            if nm and nm not in by_name:
                by_name[nm] = rec

    def match(s: dict[str, str]):
        sid = (s.get("id", "") or "").lower()
        if sid and sid in by_id:
            return by_id[sid]
        snm = (s.get("name", "") or "").lower()
        if snm in by_name:
            return by_name[snm]
        if snm:
            for nm, r in by_name.items():
                if snm in nm:
                    return r
        return None

    order: list[str] = []
    groups: dict[str, list[dict[str, str]]] = {}
    for s in seed:
        cat = s.get("cat", "Other")
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append(s)

    used: set[str] = set()
    result: list[dict] = []
    for cat in order:
        items: list[dict] = []
        for s in groups[cat]:
            rec = match(s)
            if rec is None:
                continue
            ch, cc, cn = rec
            if ch.url in used:
                continue
            used.add(ch.url)
            items.append({"ch": ch, "d": channel_record(ch, cc, cn)})
            if len(items) >= per_category:
                break
        result.append({"cat": cat, "items": items})
    return result


PAGE_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<meta name="description" content="$desc">
<meta name="keywords" content="$keywords">
<meta name="robots" content="index, follow">
$canonical<meta property="og:type" content="website">
<meta property="og:site_name" content="$title">
<meta property="og:locale" content="en_US">
<meta property="og:title" content="$title">
<meta property="og:description" content="$desc">
$og_url<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="$title">
<meta name="twitter:description" content="$desc">
$dataset_ld$website_ld$itemlist_ld<script>try{var _t=localStorage.getItem('theme');if(_t)document.documentElement.setAttribute('data-theme',_t)}catch(e){}</script>
<style>
:root,:root[data-theme=light]{color-scheme:light;--bg:#fafafa;--fg:#16181d;--muted:#6b7280;--card:#fff;--line:#e6e8eb;--accent:#2563eb}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0d0f13;--fg:#e8eaed;--muted:#9aa0a6;--card:#15181e;--line:#262a31;--accent:#6ea8fe}}
:root[data-theme=dark]{color-scheme:dark;--bg:#0d0f13;--fg:#e8eaed;--muted:#9aa0a6;--card:#15181e;--line:#262a31;--accent:#6ea8fe}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:flex-start;justify-content:center;padding:2rem;
font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
main{width:100%;max-width:960px}
h1{font-size:1.5rem;margin:0 0 .25rem;letter-spacing:-.01em}
h2{font-size:1.05rem;margin:1.75rem 0 .85rem}
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
.top{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:.25rem}
.top h1{margin:0}
.tg{min-width:0;width:38px;height:38px;padding:0;display:inline-flex;align-items:center;justify-content:center;
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:9px;flex:none}
.tg:hover{border-color:var(--accent)}
#q{width:100%;font:inherit;font-size:1rem;padding:.7rem .9rem;border:1px solid var(--line);border-radius:10px;
background:var(--card);color:var(--fg);margin-bottom:1rem}
.crumb{font-size:.85rem;color:var(--muted);margin-bottom:.85rem;display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.crumb a{cursor:pointer;color:var(--accent)}
.crumb .sep{opacity:.5}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:.55rem}
.tile{text-align:left;padding:.65rem .8rem;border:1px solid var(--line);border-radius:10px;background:var(--card);
cursor:pointer;font:inherit;color:var(--fg);width:100%;min-width:0}
.tile b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile span{font-size:.76rem;color:var(--muted)}
.ch{display:flex;gap:.75rem;align-items:center;padding:.55rem .7rem;border:1px solid var(--line);border-radius:10px;
background:var(--card);margin-bottom:.5rem}
.ch img,.ch .ph{width:46px;height:46px;flex:none;border-radius:6px;background:var(--bg)}
.ch img{object-fit:contain}
.ch .m{flex:1;min-width:0}
.ch .m b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.ch .m span{font-size:.76rem;color:var(--muted)}
.ch .a{display:flex;gap:.35rem;flex-wrap:wrap;justify-content:flex-end}
.ch .a button,.ch .a a{font:inherit;font-size:.74rem;min-width:0;padding:.32rem .55rem;border-radius:7px;
border:1px solid var(--line);background:transparent;color:var(--accent);cursor:pointer;text-decoration:none;white-space:nowrap}
.ch .a button.ok{background:#16a34a;border-color:#16a34a;color:#fff}
.note{color:var(--muted);font-size:.85rem;margin:.4rem 0 .9rem;word-break:break-all}
.feat{color:var(--muted);font-size:.8rem;margin-top:1.75rem}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.82);display:flex;align-items:center;justify-content:center;z-index:50;padding:1rem}
.modal[hidden]{display:none}
.mbox{width:min(960px,96vw);background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:0 12px 48px rgba(0,0,0,.5)}
.mhead{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.7rem .9rem}
.mhead b{font-size:.95rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mbtns{display:flex;gap:.4rem;flex:none}
.mbtns button{font:inherit;font-size:.78rem;min-width:0;padding:.35rem .7rem;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;cursor:pointer}
.mbtns button:hover{border-color:var(--accent)}
.mbox video{width:100%;aspect-ratio:16/9;background:#000;display:block}
.mfall{padding:.9rem;color:var(--muted);font-size:.85rem;border-top:1px solid var(--line);margin:0;word-break:break-all}
.mfall a{color:var(--accent)}
.datalist{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:.5rem}
.datalist a{display:block;padding:.6rem .8rem;border:1px solid var(--line);border-radius:9px;background:var(--card)}
.datalist small{display:block;color:var(--muted);font-size:.72rem;margin-top:.15rem}
.pcols{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.6rem;align-items:start}
.pcol{display:flex;flex-direction:column;gap:.5rem}
.pcol h3{font-size:.78rem;margin:0 0 .1rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);text-align:center}
.pick{border:1px solid var(--line);border-radius:10px;background:var(--card);padding:.75rem .6rem;
display:flex;flex-direction:column;gap:.4rem;align-items:center;text-align:center;justify-content:flex-start;height:100%}
.pick img,.pick .ph{width:52px;height:52px;flex:none;object-fit:contain;border-radius:8px;background:var(--bg)}
.pick b{font-size:.82rem;line-height:1.25;min-height:2.5em;display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical;overflow:hidden;align-items:center}
.pick span{font-size:.72rem;color:var(--muted);margin-top:auto}
</style>
</head>
<body>
<main>
<div class="top"><h1>$title</h1><button id="theme" class="tg" type="button" aria-label="Toggle dark mode"></button></div>
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
<section id="picksec">
<h2>Top picks</h2>
<p class="how">A ready-to-play shortlist of strong channels that are live right now.
Load this single playlist, or use the full list above.</p>
<div class="card">
<div class="label">Top picks playlist</div>
<div class="urlrow"><code id="pu">$picks_url</code><button id="pc" type="button">Copy</button></div>
</div>
<div id="picks" class="pcols"></div>
</section>
<section id="discsec">
<h2>More picks</h2>
<p class="how">A wider shortlist by genre: anime, cartoons, telenovelas, cooking,
crime, nature, education and documentaries that are live right now. The cards
below preview a few per genre; the playlist holds every live channel from the
list, so load it to get them all.</p>
<div class="card">
<div class="label">More picks playlist</div>
<div class="urlrow"><code id="du">$discover_url</code><button id="dc" type="button">Copy</button></div>
</div>
<div id="disc" class="pcols"></div>
</section>
<h2>Browse and search</h2>
<input id="q" type="search" placeholder="Search channels by name, e.g. BBC" autocomplete="off">
<div id="crumb" class="crumb"></div>
<div id="list"></div>
<section id="data">
<h2>Data and downloads</h2>
<p class="how">Want the raw data? Everything below is regenerated each run and
free to use. The JSON powers this page; the M3U files load in any player.</p>
<ul class="datalist">
<li><a href="data.json">data.json<small>All channels, top picks, more picks (JSON)</small></a></li>
<li><a href="index.m3u">index.m3u<small>Full verified playlist</small></a></li>
<li><a href="top-picks.m3u">top-picks.m3u<small>Top picks playlist</small></a></li>
<li><a href="discover.m3u">discover.m3u<small>More picks playlist (full)</small></a></li>
<li><a href="recommended_seed.json">recommended_seed.json<small>Top picks seed (JSON)</small></a></li>
<li><a href="discover_seed.json">discover_seed.json<small>More picks seed (JSON)</small></a></li>
</ul>
</section>
$featured_line<footer>Source links from <a href="https://github.com/iptv-org/iptv">iptv-org</a>.
This site stores no video, only checks which public links still play.$repo</footer>
</main>
<div id="modal" class="modal" hidden>
<div class="mbox">
<div class="mhead"><b id="mtitle"></b><div class="mbtns"><button id="mfs" type="button">Fullscreen</button><button id="mclose" type="button">Close</button></div></div>
<video id="mvideo" controls playsinline></video>
<p id="mfall" class="mfall" hidden></p>
</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
(function(){
function el(id){return document.getElementById(id)}
function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]})}
var DATA={channels:[]},BASE=new URL('.',location.href).href;
var q=el('q'),crumb=el('crumb'),list=el('list');
var nav={cc:null,cat:null};

function wireCopy(btn,src){var t;btn.addEventListener('click',function(){
navigator.clipboard.writeText(src.textContent).then(function(){
btn.textContent='Copied';btn.classList.add('ok');
clearTimeout(t);t=setTimeout(function(){btn.textContent='Copy';btn.classList.remove('ok')},1500)})})}
wireCopy(el('c'),el('u'));
wireCopy(el('pc'),el('pu'));
wireCopy(el('dc'),el('du'));

var SUN='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.2 17.2L18.6 18.6M5 19l1.4-1.4M17.2 6.8L18.6 5.4"></path></svg>';
var MOON='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"></path></svg>';
var tb=el('theme');
function sysDark(){return matchMedia('(prefers-color-scheme:dark)').matches}
function effTheme(){return document.documentElement.getAttribute('data-theme')||(sysDark()?'dark':'light')}
function setIcon(){tb.innerHTML=effTheme()==='dark'?SUN:MOON}
tb.addEventListener('click',function(){var next=effTheme()==='dark'?'light':'dark';
try{localStorage.setItem('theme',next)}catch(e){}
document.documentElement.setAttribute('data-theme',next);setIcon()});
setIcon();

document.addEventListener('error',function(e){
if(e.target&&e.target.tagName==='IMG'){e.target.style.visibility='hidden'}},true);

try{var qp=new URLSearchParams(location.search).get('q');if(qp)q.value=qp}catch(e){}

fetch('data.json').then(function(r){return r.json()}).then(function(d){DATA=d;
renderGroups(DATA.top_picks,'picks','picksec');
renderGroups(DATA.discover,'disc','discsec');
render()})
.catch(function(){list.innerHTML='<p class="note">Could not load channel data.</p>'});

function renderGroups(groups,containerId,sectionId){groups=groups||[];
var total=groups.reduce(function(a,g){return a+((g.items&&g.items.length)||0)},0);
if(!total){el(sectionId).style.display='none';return}
el(containerId).innerHTML=groups.map(function(g){
if(!g.items||!g.items.length)return '';
var cards=g.items.map(function(c){
var logo=c.l?'<img loading="lazy" src="'+esc(c.l)+'" alt="">':'<div class="ph"></div>';
return '<div class="pick">'+logo+'<b>'+esc(c.n)+'</b><span>'+esc(c.cn)+'</span></div>'}).join('');
return '<div class="pcol"><h3>'+esc(g.cat)+'</h3>'+cards+'</div>'}).join('')}

q.addEventListener('input',function(){nav={cc:null,cat:null};render()});

function byName(a,b){return a.n.toLowerCase()<b.n.toLowerCase()?-1:1}
function cnOf(cc){var f=DATA.channels.find(function(c){return c.cc===cc});return f?f.cn:cc.toUpperCase()}
function countriesList(){var m={};DATA.channels.forEach(function(c){(m[c.cc]=m[c.cc]||{cc:c.cc,cn:c.cn,n:0}).n++});
return Object.keys(m).map(function(k){return m[k]}).sort(function(a,b){return a.cn<b.cn?-1:1})}
function catsList(cc){var m={};DATA.channels.forEach(function(c){if(c.cc===cc){(m[c.g]=m[c.g]||{g:c.g,n:0}).n++}});
return Object.keys(m).map(function(k){return m[k]}).sort(function(a,b){return a.g<b.g?-1:1})}
function chans(cc,cat){return DATA.channels.filter(function(c){return c.cc===cc&&c.g===cat}).sort(byName)}

function setCrumb(parts){crumb.innerHTML='';parts.forEach(function(p,i){
if(i){var s=document.createElement('span');s.className='sep';s.textContent='/';crumb.appendChild(s)}
if(p.fn){var a=document.createElement('a');a.textContent=p.t;a.addEventListener('click',p.fn);crumb.appendChild(a)}
else{var x=document.createElement('span');x.textContent=p.t;crumb.appendChild(x)}})}

function home(){nav={cc:null,cat:null};q.value='';render()}

function card(c){var purl=BASE+'countries/'+c.cc+'.m3u';
var logo=c.l?'<img loading="lazy" src="'+esc(c.l)+'" alt="">':'<div class="ph"></div>';
var sub=esc(c.cn)+' / '+esc(c.g)+(c.i?' . '+esc(c.i):'');
return '<div class="ch">'+logo+'<div class="m"><b>'+esc(c.n)+'</b><span>'+sub+'</span></div>'+
'<div class="a"><button class="pl" data-u="'+esc(c.u)+'" data-n="'+esc(c.n)+'">Play</button>'+
'<button class="cp" data-u="'+esc(c.u)+'">Copy URL</button>'+
'<a href="'+esc(purl)+'" target="_blank" rel="noopener">Country list</a></div></div>'}

function render(){var query=q.value.trim().toLowerCase();
if(query)return renderSearch(query);
if(!nav.cc)return renderCountries();
if(!nav.cat)return renderCats();
return renderChans()}

function renderCountries(){setCrumb([{t:'All countries'}]);list.className='grid';
list.innerHTML=countriesList().map(function(c){
return '<button class="tile" data-cc="'+esc(c.cc)+'"><b>'+esc(c.cn)+'</b><span>'+c.n+' channels</span></button>'}).join('')}

function renderCats(){setCrumb([{t:'All countries',fn:home},{t:cnOf(nav.cc)}]);list.className='grid';
list.innerHTML=catsList(nav.cc).map(function(c){
return '<button class="tile" data-cat="'+esc(c.g)+'"><b>'+esc(c.g)+'</b><span>'+c.n+' channels</span></button>'}).join('')}

function renderChans(){var cc=nav.cc;
setCrumb([{t:'All countries',fn:home},{t:cnOf(cc),fn:function(){nav.cat=null;render()}},{t:nav.cat}]);
list.className='';
var head='<p class="note">Country playlist: <a href="'+BASE+'countries/'+cc+'.m3u" target="_blank" rel="noopener">'+
BASE+'countries/'+cc+'.m3u</a></p>';
list.innerHTML=head+chans(cc,nav.cat).map(card).join('')}

function renderSearch(query){setCrumb([{t:'Search'}]);list.className='';
var res=DATA.channels.filter(function(c){return c.n.toLowerCase().indexOf(query)>-1}).sort(byName);
var cap=res.slice(0,200);
var note='<p class="note">'+res.length+' result'+(res.length===1?'':'s')+(res.length>200?' (showing first 200)':'')+'</p>';
list.innerHTML=res.length?note+cap.map(card).join(''):note+'<p class="note">No channels match.</p>'}

list.addEventListener('click',function(e){
var tile=e.target.closest('.tile');
if(tile){if(tile.dataset.cc){nav.cc=tile.dataset.cc;nav.cat=null}else if(tile.dataset.cat){nav.cat=tile.dataset.cat}return render()}
var pl=e.target.closest('.pl');
if(pl){return play(pl.dataset.u,pl.dataset.n)}
var cp=e.target.closest('.cp');
if(cp){navigator.clipboard.writeText(cp.dataset.u).then(function(){var o=cp.textContent;
cp.textContent='Copied';cp.classList.add('ok');setTimeout(function(){cp.textContent=o;cp.classList.remove('ok')},1200)})}});

// In-page HLS player (best effort: CORS/geo/mixed-content streams fall back).
var modal=el('modal'),mvideo=el('mvideo'),mtitle=el('mtitle'),mfall=el('mfall'),hls=null,ptimer;
function stopPlay(){if(hls){try{hls.destroy()}catch(e){}hls=null}try{mvideo.pause()}catch(e){}
mvideo.removeAttribute('src');mvideo.load();clearTimeout(ptimer)}
function closeModal(){stopPlay();modal.hidden=true}
function showFall(url){mfall.hidden=false;
mfall.innerHTML='This stream would not play in the browser (usually CORS, a geo-block, or an http source). '+
'Open it in VLC, TiViMate, Kodi, or another M3U player: <a href="'+esc(url)+'" target="_blank" rel="noopener">'+esc(url)+'</a>'}
function play(url,name){mtitle.textContent=name;mfall.hidden=true;modal.hidden=false;stopPlay();
var v=mvideo,settled=false;
function ok(){settled=true;clearTimeout(ptimer);v.play().catch(function(){})}
function fail(){if(!settled){settled=true;clearTimeout(ptimer);showFall(url)}}
ptimer=setTimeout(function(){if(v.paused)fail()},12000);
if(v.canPlayType('application/vnd.apple.mpegurl')){v.src=url;
v.addEventListener('loadedmetadata',ok,{once:true});v.addEventListener('error',fail,{once:true})}
else if(window.Hls&&Hls.isSupported()){hls=new Hls({maxBufferLength:10});hls.loadSource(url);hls.attachMedia(v);
hls.on(Hls.Events.MANIFEST_PARSED,ok);hls.on(Hls.Events.ERROR,function(_,d){if(d&&d.fatal)fail()})}
else fail()}
el('mclose').addEventListener('click',closeModal);
modal.addEventListener('click',function(e){if(e.target===modal)closeModal()});
document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!modal.hidden)closeModal()});
el('mfs').addEventListener('click',function(){var v=mvideo;
(v.requestFullscreen||v.webkitRequestFullscreen||v.webkitEnterFullscreen||function(){}).call(v)});
})();
</script>
</body>
</html>
"""
)


def clean_title(name: str) -> str:
    """Clean brand name for SEO: drop resolution/marker suffixes, fold to ASCII.

    Folding (Clasicas, Acao) keeps the generated HTML ASCII while staying a valid
    search term; the cards themselves render the original names from data.json.
    """
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", name).strip()
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return folded.strip()


def build_html(
    *,
    total: int,
    countries: int,
    updated: str,
    iso: str,
    site_url: str | None,
    repo_url: str | None,
    featured: list[str] | None = None,
) -> str:
    title = "iptv-curated"
    desc = (
        "Search and browse a free, auto-verified IPTV playlist. Find live TV "
        "channels by name, filter by country and category, and copy direct stream "
        "URLs for VLC, TiViMate, and Kodi."
    )
    playlist = f"{site_url}/index.m3u" if site_url else "index.m3u"
    picks_url = f"{site_url}/top-picks.m3u" if site_url else "top-picks.m3u"
    discover_url = f"{site_url}/discover.m3u" if site_url else "discover.m3u"
    canonical = f'<link rel="canonical" href="{html.escape(site_url)}/">\n' if site_url else ""
    og_url = f'<meta property="og:url" content="{html.escape(site_url)}/">\n' if site_url else ""
    repo = (
        f' <a href="{html.escape(repo_url)}">Source on GitHub</a>.' if repo_url else ""
    )

    def ld(obj: dict) -> str:
        return '<script type="application/ld+json">' + json.dumps(obj) + "</script>\n"

    # Featured channel names drive dynamic, channel-specific SEO.
    names: list[str] = []
    for n in featured or []:
        c = clean_title(n)
        if c and c not in names:
            names.append(c)

    base_kw = (
        "IPTV, M3U playlist, free IPTV, live TV, IPTV channels, IPTV by country, "
        "channel search, VLC, TiViMate, Kodi"
    )
    chan_kw = ", ".join(names[:45])
    intent_kw = ", ".join(f"watch {n} free" for n in names[:10])
    keywords = ", ".join(p for p in [base_kw, chan_kw, intent_kw] if p)

    # Dataset: the rich-result type Google supports here (Google Dataset Search).
    dataset: dict = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": desc,
        "dateModified": iso,
        "isAccessibleForFree": True,
        "license": "https://unlicense.org/",
        "creator": {
            "@type": "Organization",
            "name": "iptv-org",
            "url": "https://github.com/iptv-org/iptv",
        },
    }
    if site_url:
        dataset["url"] = f"{site_url}/"
        dataset["keywords"] = names[:25]
        dataset["distribution"] = [
            {
                "@type": "DataDownload",
                "name": label,
                "encodingFormat": "application/x-mpegurl",
                "contentUrl": url,
            }
            for label, url in (
                ("Full playlist", playlist),
                ("Top picks", picks_url),
                ("More picks", discover_url),
            )
        ]
    dataset_ld = ld(dataset)

    # WebSite name signal. The sitelinks search box was retired by Google in 2024;
    # the SearchAction no longer renders but is harmless and documents the ?q= API.
    website_ld = ""
    if site_url:
        website_ld = ld(
            {
                "@context": "https://schema.org",
                "@type": "WebSite",
                "name": title,
                "url": f"{site_url}/",
                "potentialAction": {
                    "@type": "SearchAction",
                    "target": {
                        "@type": "EntryPoint",
                        "urlTemplate": f"{site_url}/?q={{search_term_string}}",
                    },
                    "query-input": "required name=search_term_string",
                },
            }
        )

    # ItemList of featured channels; each item links back to its on-site search so
    # the list points to same-domain pages (carousel requirement).
    itemlist_ld = ""
    if names and site_url:
        itemlist_ld = ld(
            {
                "@context": "https://schema.org",
                "@type": "ItemList",
                "name": "Featured live channels",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "name": n,
                        "url": f"{site_url}/?q={quote(n)}",
                    }
                    for i, n in enumerate(names[:35])
                ],
            }
        )

    featured_line = ""
    if names:
        featured_line = (
            '<p class="feat">Featured live channels: '
            + html.escape(", ".join(names[:35]))
            + ".</p>\n"
        )

    return PAGE_TEMPLATE.substitute(
        title=title,
        desc=desc,
        url=html.escape(playlist),
        picks_url=html.escape(picks_url),
        discover_url=html.escape(discover_url),
        canonical=canonical,
        og_url=og_url,
        dataset_ld=dataset_ld,
        website_ld=website_ld,
        itemlist_ld=itemlist_ld,
        keywords=html.escape(keywords),
        featured_line=featured_line,
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
    parser.add_argument(
        "--recommended-seed",
        default="recommended_seed.json",
        help="Curated seed list of channels for the Top picks section.",
    )
    parser.add_argument(
        "--discover-seed",
        default="discover_seed.json",
        help="Curated seed list of channels for the Discover (more picks) section.",
    )
    parser.add_argument(
        "--picks-per-category",
        type=int,
        default=3,
        help="Top picks channels kept per seed category.",
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
    names = load_country_names(args.country_names)
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

    now = datetime.now(timezone.utc)
    outdir = os.path.dirname(args.output) or "."

    # Public per-country playlists (keep original category grouping) so the
    # browser app can offer a per-country URL, and the channel dataset it filters.
    pub_countries = os.path.join(outdir, COUNTRIES_DIR)
    for cc, channels in chosen.items():
        if channels:
            write_channels(os.path.join(pub_countries, f"{cc}.m3u"), channels)

    # Curated sections: match a seed against the playable set, per category. The
    # playlist gets every live seed channel (m3u_per_cat); the cards show only a
    # preview (cards_per_cat) so the page stays compact.
    def emit_section(
        seed_path: str, m3u_name: str, label: str, cards_per_cat: int, m3u_per_cat: int
    ) -> tuple[list[dict], list[str]]:
        seed = load_seed(seed_path)
        if not seed:
            return [], []
        if os.path.isfile(seed_path):
            with open(seed_path, "r", encoding="utf-8") as src:
                with open(os.path.join(outdir, os.path.basename(seed_path)), "w", encoding="utf-8") as dst:
                    dst.write(src.read())  # publish the seed for transparency
        groups = build_top_picks(chosen, seed, names, m3u_per_cat)
        flat = [(g["cat"], it["ch"]) for g in groups for it in g["items"]]
        if flat:
            write_channels(
                os.path.join(outdir, m3u_name),
                [set_group_title(ch, cat) for cat, ch in flat],
            )
            cards_n = sum(min(len(g["items"]), cards_per_cat) for g in groups)
            print(
                f"{label}: {cards_n} cards, {len(flat)} in {m3u_name}", file=sys.stderr
            )
        cards = [
            {"cat": g["cat"], "items": [it["d"] for it in g["items"][:cards_per_cat]]}
            for g in groups
        ]
        all_names = [it["d"]["n"] for g in groups for it in g["items"]]
        return cards, all_names

    per = args.picks_per_category
    top_picks_data, top_names = emit_section(args.recommended_seed, "top-picks.m3u", "top picks", per, per)
    discover_data, disc_names = emit_section(args.discover_seed, "discover.m3u", "discover", per, 99)
    featured_names = top_names + disc_names

    write_data_json(
        os.path.join(outdir, "data.json"),
        chosen,
        names,
        now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        top_picks=top_picks_data,
        discover=discover_data,
    )

    if not args.no_html:
        countries_published = sum(1 for n in new_counts.values() if n > 0)
        page = build_html(
            total=total,
            countries=countries_published,
            updated=now.strftime("%Y-%m-%d %H:%M"),
            iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            site_url=args.site_url.rstrip("/") if args.site_url else None,
            repo_url=args.repo_url,
            featured=featured_names,
        )
        html_path = os.path.join(outdir, "index.html")
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
