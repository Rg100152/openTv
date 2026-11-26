"""openTv - a lightweight Flask app for legal Free-to-Air Indian live TV.

Fetches the public iptv-org India playlist once at startup, caches it in
memory, and renders channels directly through Jinja2. No background polling,
no heavy dependencies. Falls back to Prasar Bharati (DD) channels offline.
"""

from __future__ import annotations

import re
import time
from typing import Dict, List

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

PLAYLIST_URL = "https://iptv-org.github.io/iptv/countries/in.m3u"
REPO_URL = "https://github.com/Rg100152/openTv"
CACHE_SECONDS = 600          # 10 minutes; no polling from the browser
REQUEST_TIMEOUT = 8          # short timeout keeps startup fast
USER_AGENT = "openTv/1.0 (+https://github.com/Rg100152/openTv)"

# Official Prasar Bharati channels, taken from the same public FTA playlist.
# Used only when the remote playlist cannot be reached.
FALLBACK_CHANNELS: List[Dict[str, str]] = [
    {
        "name": "DD National HD",
        "logo": "https://ltsk-cdn.s3.eu-west-1.amazonaws.com/jumpstart/Temp_Live/cdn/HLS/Channel/transparentImages/DD%20National.png",
        "group": "Entertainment",
        "bucket": "entertainment",
        "stream": "https://fta.ektv.in/live/15/master.m3u8",
    },
    {
        "name": "DD News",
        "logo": "https://ltsk-cdn.s3.eu-west-1.amazonaws.com/jumpstart/Temp_Live/cdn/HLS/Channel/transparentImages/DD%20News%20HD.png",
        "group": "News",
        "bucket": "news",
        "stream": "https://mumbai-edge.smartplaytv.in/DDNews/index.m3u8",
    },
    {
        "name": "DD Sports",
        "logo": "https://dtil.tmsimg.com/assets/s158255_ld_h15_aa.png?lock=720x540",
        "group": "Sports",
        "bucket": "entertainment",
        "stream": "https://d3qs3d2rkhfqrt.cloudfront.net/out/v1/b17adfe543354fdd8d189b110617cddd/index.m3u8",
    },
    {
        "name": "DD India",
        "logo": "https://dtil.tmsimg.com/assets/s158251_ld_h15_aa.png?lock=720x540",
        "group": "General",
        "bucket": "entertainment",
        "stream": "https://fta.ektv.in/live/119/119_0.m3u8",
    },
    {
        "name": "DD Bharati",
        "logo": "https://i.imgur.com/4tfUIEo.png",
        "group": "Culture",
        "bucket": "devotional",
        "stream": "https://cdn.pishow.tv/ott/live/10/master.m3u8",
    },
    {
        "name": "DD Kisan",
        "logo": "https://i.imgur.com/x56WJEa.png",
        "group": "Education",
        "bucket": "entertainment",
        "stream": "https://mumbai-edge.smartplaytv.in/DDKisan/index.m3u8",
    },
]

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_RESOLUTION_RE = re.compile(r"\s*\([0-9]+p\)\s*$", re.I)

_BUCKET_RULES = (
    ("news", ("news", "politics", "business")),
    ("music", ("music", "radio")),
    ("movies", ("movie", "cinema", "film")),
    ("devotional", ("religious", "devotion", "spiritual", "bhajan", "culture")),
    ("kids", ("kids", "children", "cartoon", "kids tv")),
)


def _bucket(group: str) -> str:
    """Map a playlist group-title onto one of the seven UI filter pills."""
    text = (group or "").lower()
    for bucket, keywords in _BUCKET_RULES:
        if any(word in text for word in keywords):
            return bucket
    return "entertainment"  # includes General, Sports, Regional, Education...


def parse_playlist(text: str) -> List[Dict[str, str]]:
    """Parse an M3U playlist into lightweight channel dictionaries."""
    lines = text.splitlines()
    found: Dict[str, Dict[str, str]] = {}

    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue

        # The stream URL is the first non-comment line after #EXTINF.
        stream = ""
        for following in lines[index + 1: index + 5]:
            if not following or following.startswith("#"):
                continue
            stream = following.strip()
            break
        if not stream.lower().startswith(("http://", "https://")) or ".m3u8" not in stream.lower():
            continue

        attrs = dict(_ATTR_RE.findall(line))
        raw_name = line[line.rfind(",") + 1:].strip() if "," in line else "Unknown Channel"
        name = _RESOLUTION_RE.sub("", raw_name).strip() or "Unknown Channel"
        key = name.lower()

        is_hd = "hd" in raw_name.lower()
        previous = found.get(key)
        if previous and not (is_hd and "hd" not in previous["name"].lower()):
            continue  # skip SD duplicate / SD-first ordering

        found[key] = {
            "name": name,
            "logo": attrs.get("tvg-logo", ""),
            "group": attrs.get("group-title", "General") or "General",
            "bucket": _bucket(attrs.get("group-title", "")),
            "stream": stream,
        }

    return list(found.values())


_channel_cache: List[Dict[str, str]] = []
_cache_time = 0.0
_cache_source = "fallback"


def get_channels() -> List[Dict[str, str]]:
    """Return cached channels, refreshing at most once every 10 minutes."""
    global _cache_time, _cache_source

    if _channel_cache and time.monotonic() - _cache_time < CACHE_SECONDS:
        return list(_channel_cache)

    try:
        response = requests.get(
            PLAYLIST_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        channels = parse_playlist(response.text)
        if len(channels) < 5:
            raise ValueError("playlist returned too few channels")
        source = "iptv-org FTA playlist"
    except Exception as exc:  # offline, slow, or malformed playlist
        app.logger.warning("Using built-in DD fallback channels: %s", exc)
        channels = [dict(channel) for channel in FALLBACK_CHANNELS]
        source = "built-in DD fallback"

    _channel_cache[:] = channels
    _cache_time = time.monotonic()
    _cache_source = source
    return list(_channel_cache)


# One fetch at startup; every page render afterwards is pure in-memory work.
CHANNELS = get_channels()


def _featured(channels: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Put well-known DD channels first in the quick-switch sidebar."""
    preferred = ("DD National", "DD News", "DD Sports", "DD India", "DD Bharati", "DD Kisan")
    picked: List[Dict[str, str]] = []

    for wanted in preferred:
        for channel in channels:
            if channel["name"].lower().startswith(wanted.lower()) and channel not in picked:
                picked.append(channel)
                break

    for channel in channels:
        if len(picked) >= 12:
            break
        if channel not in picked:
            picked.append(channel)

    return picked[:12]


FEATURED_CHANNELS = _featured(CHANNELS)


@app.route("/")
def index():
    return render_template(
        "index.html",
        channels=CHANNELS,
        featured=FEATURED_CHANNELS,
        channel_count=len(CHANNELS),
        source=_cache_source,
        repo_url=REPO_URL,
    )


@app.route("/api/channels")
def api_channels():
    """Optional lightweight JSON endpoint; the UI does not poll it."""
    return jsonify({"count": len(CHANNELS), "source": _cache_source, "channels": CHANNELS})


if __name__ == "__main__":
    # 0.0.0.0 also allows testing from a phone on the same Wi-Fi.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
