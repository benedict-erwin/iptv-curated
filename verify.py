#!/usr/bin/env python3
"""Verify IPTV M3U playlists and emit only channels that actually play.

Reads a playlist (local file or URL), checks each stream with ffprobe to
confirm a real video stream exists (not just an HTTP 200), and writes a
filtered playable.m3u. Checks run concurrently with bounded concurrency and a
per-channel timeout. Progress and summary go to stderr; the playlist is the
only thing written to --output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Channel:
    """One playlist entry: its tag lines (#EXTINF + friends) and stream URL."""

    tags: list[str] = field(default_factory=list)
    url: str = ""

    def render(self) -> str:
        return "\n".join([*self.tags, self.url])


def parse_m3u(text: str) -> tuple[str, list[Channel]]:
    """Split playlist text into (#EXTM3U header, list of channels).

    Each channel groups its #EXTINF line plus any associated tags
    (#EXTVLCOPT, #EXTGRP, #KODIPROP, ...) with the following URL line.
    """
    header = "#EXTM3U"
    channels: list[Channel] = []
    pending: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header = line  # preserve any header attributes
            continue
        if line.startswith("#"):
            pending.append(line)
            continue
        # Non-comment line = stream URL; close out the current channel.
        channels.append(Channel(tags=pending, url=line))
        pending = []

    return header, channels


def load_playlist(input_path: str | None, url: str | None) -> str:
    if input_path:
        with open(input_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": "iptv-verify/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def has_video_stream(url: str, timeout: float) -> bool:
    """True if ffprobe finds a video stream within the timeout."""
    # -rw_timeout is microseconds and must precede the input (-i).
    rw_timeout = str(int(timeout * 1_000_000))
    args = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-rw_timeout", rw_timeout,
        "-i", url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0 and b"video" in stdout


async def verify_all(
    channels: list[Channel], workers: int, timeout: float
) -> list[Channel]:
    semaphore = asyncio.Semaphore(workers)
    total = len(channels)
    done = 0
    passed = 0

    async def check(channel: Channel) -> Channel | None:
        nonlocal done, passed
        async with semaphore:
            ok = await has_video_stream(channel.url, timeout)
        done += 1
        if ok:
            passed += 1
        print(
            f"\r[{done}/{total}] playable: {passed}",
            end="",
            file=sys.stderr,
            flush=True,
        )
        return channel if ok else None

    results = await asyncio.gather(*(check(c) for c in channels))
    print("", file=sys.stderr)  # newline after the progress line
    return [c for c in results if c is not None]


def write_playlist(path: str, header: str, channels: list[Channel]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for channel in channels:
            fh.write(channel.render() + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify an IPTV M3U playlist and emit only playable channels."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="Path to a local .m3u file.")
    source.add_argument("--url", help="URL of an .m3u playlist to fetch.")
    parser.add_argument(
        "--output", default="playable.m3u", help="Output path (default: playable.m3u)."
    )
    parser.add_argument(
        "--workers", type=int, default=40, help="Max concurrent checks (default: 40)."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Per-channel timeout in seconds (default: 8).",
    )
    parser.add_argument(
        "--skip-empty",
        action="store_true",
        help="If no channels are playable, write nothing (and remove a stale "
        "output file) instead of emitting a header-only playlist.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    if shutil.which("ffprobe") is None:
        print("error: ffprobe not found on PATH (install ffmpeg).", file=sys.stderr)
        return 2

    try:
        text = load_playlist(args.input, args.url)
    except OSError as exc:
        print(f"error: could not read playlist: {exc}", file=sys.stderr)
        return 1

    header, channels = parse_m3u(text)
    if not channels:
        print("error: no channels found in playlist.", file=sys.stderr)
        return 1

    print(f"parsed {len(channels)} channels; verifying...", file=sys.stderr)
    playable = await verify_all(channels, args.workers, args.timeout)

    if not playable and args.skip_empty:
        # Nothing alive: leave no header-only file behind for the merge step.
        if os.path.exists(args.output):
            os.remove(args.output)
        print(f"done: 0/{len(channels)} playable -> skipped {args.output}", file=sys.stderr)
        return 0

    write_playlist(args.output, header, playable)
    print(
        f"done: {len(playable)}/{len(channels)} playable -> {args.output}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
