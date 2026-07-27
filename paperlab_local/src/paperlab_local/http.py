"""Polite HTTP helpers shared by all sources.

Per-host throttling keeps arXiv (3s) and Semantic Scholar (1.1s) happy and
makes the USENIX quirk (empty body when compressed) explicit.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_MIN_INTERVAL = {
    "export.arxiv.org": 3.0,
    "api.semanticscholar.org": 1.2,
    "dblp.org": 1.0,
    "web.archive.org": 1.0,
    "archive.org": 1.0,
}

_LAST: dict[str, float] = {}


def _throttle(host: str) -> None:
    wait = _MIN_INTERVAL.get(host, 0.2)
    # Authenticated S2 requests get a dedicated 1 RPS limit instead of the
    # shared anonymous pool, so we can keep the interval at 1.0s with a key.
    if host == "api.semanticscholar.org" and s2_api_key():
        wait = 1.0
    last = _LAST.get(host, 0.0)
    delta = time.monotonic() - last
    if delta < wait:
        time.sleep(wait - delta)
    _LAST[host] = time.monotonic()


def s2_api_key() -> str:
    """Semantic Scholar API key from env, if configured."""
    return os.environ.get("S2_API_KEY", "")


def _s2_headers() -> dict:
    """Headers for Semantic Scholar, including API key when available."""
    headers: dict[str, str] = {}
    key = s2_api_key()
    if key:
        headers["x-api-key"] = key
    return headers


def fetch_text(url: str, *, compressed: bool = True, max_time: int = 40, retries: int = 2) -> str:
    """GET `url` and return response text. Raises RuntimeError on persistent failure."""
    host = urlparse(url).hostname or ""
    headers = {"User-Agent": UA}
    if host == "api.semanticscholar.org":
        headers.update(_s2_headers())
    # USENIX returns an empty body when it sees Accept-Encoding gzip, so callers
    # hitting USENIX pass compressed=False to force identity encoding.
    if not compressed:
        headers["Accept-Encoding"] = "identity"
    last_err = "unknown"
    for attempt in range(retries + 1):
        _throttle(host)
        try:
            r = requests.get(url, headers=headers, timeout=max_time)
            if r.status_code == 200 and r.text:
                return r.text
            last_err = f"HTTP {r.status_code} (len={len(r.text)})"
            # Retrying a permanent missing/forbidden proceedings URL makes a
            # full 23-venue scan take hours. Retry only transient HTTP errors.
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                break
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last_err}")


def fetch_bytes(url: str, dest, *, max_time: int = 120, retries: int = 2) -> int:
    """Stream `url` to a Path-like `dest`. Returns bytes written."""
    from pathlib import Path

    host = urlparse(url).hostname or ""
    headers = {"User-Agent": UA}
    dest = Path(dest)
    last_err = "unknown"
    for attempt in range(retries + 1):
        _throttle(host)
        try:
            with requests.get(url, headers=headers, timeout=max_time, stream=True) as r:
                if r.status_code == 200:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    n = 0
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            if chunk:
                                f.write(chunk)
                                n += len(chunk)
                    if n > 0:
                        return n
                    last_err = "empty body"
                else:
                    last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"download failed for {url}: {last_err}")
