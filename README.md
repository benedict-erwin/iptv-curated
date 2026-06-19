# iptv-curated

Verify large IPTV M3U playlists (e.g. from [iptv-org/iptv](https://github.com/iptv-org/iptv))
and emit a filtered playlist containing only channels that actually play.

Raw playlists carry thousands of channels, many dead. This tool probes each
stream with `ffprobe` to confirm a **real video stream** exists (not just an
HTTP 200), then writes a clean playlist any M3U player (VLC, TiViMate, Kodi)
can load with confidence. The published `index.m3u` groups channels by country.

## Requirements

- **Python 3.11+**
- **ffmpeg** (provides the `ffprobe` binary) on your `PATH`
- **openvpn** - only for the full `build_all.sh` run (per-country VPN); not
  needed for `verify.py` or `run_local.sh`

Install:

```bash
# macOS
brew install ffmpeg
brew install openvpn      # only if you will run build_all.sh

# Debian / Ubuntu
sudo apt-get install ffmpeg
sudo apt-get install openvpn   # only if you will run build_all.sh

# verify ffprobe is on PATH
ffprobe -version
```

## Setup - run in a virtual environment (venv)

Use a venv so dependencies stay isolated and don't pollute your system Python.
`verify.py` is stdlib-only today, but the venv keeps the project self-contained
and ready if deps are added later.

```bash
# 1. create the venv (creates a ./.venv directory)
python3 -m venv .venv

# 2. activate it
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows (PowerShell: .venv\Scripts\Activate.ps1)

# 3. install dependencies into the venv
pip install -r requirements.txt

# ...work...

# 4. leave the venv when done
deactivate
```

While the venv is active your shell prompt shows `(.venv)`, and `python` /
`pip` resolve to the venv's copies - nothing lands in the global environment.
Add `.venv/` to your `.gitignore` so the environment isn't committed.

## Usage

Run with the venv activated:

```bash
# from a URL (default iptv-org playlist)
python verify.py --url https://iptv-org.github.io/iptv/index.m3u --output playable.m3u

# from a local file, tuned for speed
python verify.py --input index.m3u --output playable.m3u --workers 60 --timeout 6
```

Without activating, you can call the venv's interpreter directly:

```bash
.venv/bin/python verify.py --url https://iptv-org.github.io/iptv/index.m3u
```

### CLI flags

| Flag           | Default        | Description                                  |
|----------------|----------------|----------------------------------------------|
| `--input`      | -              | Path to a local `.m3u` file.                 |
| `--url`        | -              | URL of an `.m3u` playlist to fetch.          |
| `--output`     | `playable.m3u` | Where to write the filtered playlist.        |
| `--workers`    | `40`           | Max concurrent stream checks.                |
| `--timeout`    | `8`            | Per-channel timeout in seconds.              |
| `--skip-empty` | off            | If nothing is playable, write no file (and remove a stale one) instead of a header-only playlist. |

`--input` and `--url` are mutually exclusive; exactly one is required.

Progress and the final summary print to **stderr**; only the playlist is
written to `--output`. Exit code is non-zero on missing `ffprobe`, an
unreadable input, or an empty playlist.

## Generate a playlist locally (before pushing)

There are two local entry points:

- `run_local.sh` - quick, no VPN. Verifies from your own IP.
- `build_all.sh` - full, every country, each behind a same-country VPN where one
  exists. Mirrors the CI pipeline; use it as a manual fallback if CI is down.

### Quick run (no VPN)

`run_local.sh` checks `ffprobe`, creates and activates `.venv`, installs deps,
then verifies the full iptv-org playlist into `m3u/index.m3u`.

```bash
./run_local.sh
```

Running from your own machine uses your real IP, so channels in your own country
verify correctly without any VPN. Other countries' geo-locked streams still read
as dead (the same as the CI `direct` mode). The full playlist (~10k+ channels)
takes roughly 30-40 minutes; raise `--workers` to go faster.

Any arguments are passed straight through to `verify.py`, so you can do a quick
single-country test first:

```bash
./run_local.sh --url https://iptv-org.github.io/iptv/countries/id.m3u --output m3u/countries/id.m3u --workers 60
```

If your default `python3` is not 3.11+, point the script at another one:

```bash
PYTHON=python3.12 ./run_local.sh
```

### Full run (all countries, with VPN)

`build_all.sh` does what CI does, on your machine: fetch the VPNGate list, then
for every iptv-org country connect a same-country VPN if a relay exists (direct
otherwise), verify it, and merge into `m3u/index.m3u`.

```bash
brew install openvpn   # one-time; ffmpeg too if missing
./build_all.sh
```

This needs `openvpn` and `sudo` (OpenVPN sets up the tunnel), runs countries
serially, and can take hours. VPNGate only covers a few dozen countries, so most
countries still fall back to `direct` from your own IP, the same limitation as
CI. Any country whose VPN fails is verified direct, so the build always finishes.
On macOS OpenVPN routing can need extra setup; failures degrade to direct.

### Where files go

```
m3u/index.m3u          merged playlist (tracked - this is what you push)
m3u/index.html         landing page with the URL, stats, last-updated
m3u/countries/<cc>.m3u per-country results (tracked)
ovpn/                  VPNGate cache + per-country .ovpn (gitignored)
.localstate/           guard state across local runs (gitignored)
```

The landing page links to an absolute URL when you set `SITE_URL` (and
`REPO_URL`), otherwise it uses a relative link:

```bash
SITE_URL=https://<user>.github.io/<repo> REPO_URL=https://github.com/<user>/<repo> ./build_all.sh
```

Once `m3u/index.m3u` looks good, push it, or let the scheduled CI rebuild and
publish to Pages.

## How "playable" is decided

A channel passes only if `ffprobe` reports a video stream
(`codec_type=video`) within the timeout. Some dead streams return HTTP 200
with a splash/error page - the stream check is why status codes alone aren't
trusted.

> **Geo-blocked channels** read as dead unless you run behind a matching VPN.
> This is expected and not worked around.

## Be a good network citizen

Concurrency is bounded (`--workers`) and every check has a timeout. Don't
hammer source servers; scheduled runs tighter than a few hours are discouraged.

## Automated build (GitHub Actions, every 6 hours)

A scheduled workflow (`.github/workflows/verify.yml`) rebuilds a global
`index.m3u` every ~6 hours and publishes it to **GitHub Pages**, along with an
`index.html` landing page (playlist URL, channel/country counts, last-updated):

```
https://<user>.github.io/<repo>/            landing page
https://<user>.github.io/<repo>/index.m3u   playlist for your player
```

Enable Pages once under Settings - Pages, source = branch `gh-pages`. The first
workflow run creates that branch; after you enable Pages, every run republishes.

### Pipeline

1. **plan** - fetch the [VPNGate](https://www.vpngate.net/) server list once and
   the iptv-org country list, then build a job matrix of every country
   (`mode: vpn` if VPNGate has a server there, else `mode: direct`).
2. **verify** (one matrix job per country) - for `vpn` countries, connect
   OpenVPN to a same-country VPNGate relay (verifying the exit IP's country
   before trusting it), then run `verify.py` on that country's
   `countries/<cc>.m3u`. Countries without a VPNGate relay are checked
   `direct` from the US runner.
3. **merge** - combine all per-country results into `index.m3u` (de-duplicated
   by stream URL), relabel each channel's `group-title` so players group by
   country (`Indonesia / News`), write the `index.html` landing page, and deploy
   both to Pages.

### Why VPN per country

Stream servers geo-block by the **client's** country, and a runner has one IP in
one country. Verifying every country's geo-locked streams therefore requires
routing each country's checks through a VPN exit in that country - that's what
the matrix + VPNGate does.

Caveats:
- VPNGate coverage is partial. Many countries have no relay; those fall back to
  `direct` (US) and their geo-locked streams read as dead.
- Free relays are slow and flaky. VPN jobs use a longer `--timeout`, but some
  timeouts still register as dead.

### Stale-result guards (`merge.py`)

A flaky run or dead VPN must not wipe a good playlist:

- Per-country: if a country's playable count collapses vs the last run (below a
  floor or down more than `--country-drop`), the previous country playlist is
  reused.
- Global: if the assembled total collapses (below `--min-total` or down more
  than `--total-drop`), the previous `index.m3u` is kept and nothing new is
  published.

Previous per-country results and counts are kept under `state/` on the
`gh-pages` branch to power these comparisons.

### Requirements

- Repo must be **public** - ~200 country jobs every 6h need unlimited Actions
  minutes (free for public repos) and Pages.
- Enable GitHub Pages with the **gh-pages branch** as source (the deploy step
  pushes there).

### Run a country locally behind your own VPN

```bash
# pick a server and connect (root needed for the tun device)
python vpngate.py get-config id --out ovpn/id.ovpn   # add --csv ovpn/vpngate.csv to reuse a cached list
sudo openvpn --config ovpn/id.ovpn --daemon
python verify.py --url https://iptv-org.github.io/iptv/countries/id.m3u --output m3u/countries/id.m3u --timeout 15
sudo pkill openvpn
```
