"""Direct webpage scraping for abstracts that APIs cannot provide.

Some proceedings sites expose abstracts as plain HTML but are not indexed by
OpenAlex or Semantic Scholar (or those APIs are rate-limited).  These
scrapers read the abstract directly from the publisher's page, using the
``source_url`` already stored in ``paper_sources.raw_json``.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..http import fetch_text


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def scrape(url: str) -> str | None:
    """Fetch the abstract from a proceedings page, or ``None`` if unreadable.

    Dispatches by host.  Each scraper is robust to minor markup changes:
    it tries multiple regex patterns before giving up.
    """
    host = urlparse(url).hostname or ""
    if "papers.nips.cc" in host:
        return _neurips(url)
    if "aclanthology.org" in host:
        return _acl(url)
    if "proceedings.mlr.press" in host or "mlr.press" in host:
        return _pmlr(url)
    if "openaccess.thecvf.com" in host:
        return _cvf(url)
    return None


def _neurips(url: str) -> str | None:
    try:
        html = fetch_text(url, max_time=20, retries=1)
    except RuntimeError:
        return None
    for pattern in (r"paper-abstract[^>]*>(.*?)</p>", r"<h4>Abstract</h4>\s*<p>(.*?)</p>"):
        m = re.search(pattern, html, re.S)
        if m:
            abstract = _strip_html(m.group(1))
            if len(abstract) > 30:
                return abstract
    return None


def _acl(url: str) -> str | None:
    try:
        html = fetch_text(url, max_time=20, retries=1)
    except RuntimeError:
        return None
    for pattern in (r"acl-abstract[^>]*>.*?<span>(.*?)</span>", r"class=\"abstract\"[^>]*>(.*?)</div>"):
        m = re.search(pattern, html, re.S)
        if m:
            abstract = _strip_html(m.group(1))
            if len(abstract) > 30:
                return abstract
    return None


def _pmlr(url: str) -> str | None:
    try:
        html = fetch_text(url, max_time=20, retries=1)
    except RuntimeError:
        return None
    m = re.search(r'name="description" content="(.*?)"', html, re.I | re.S)
    if m:
        abstract = m.group(1).strip()
        if len(abstract) > 30:
            return abstract
    return None


def _cvf(url: str) -> str | None:
    try:
        html = fetch_text(url, max_time=20, retries=1)
    except RuntimeError:
        return None
    for pattern in (r'<div id="abstract">(.*?)</div>', r'name="citation_abstract" content="(.*?)"'):
        m = re.search(pattern, html, re.S | re.I)
        if m:
            abstract = _strip_html(m.group(1))
            if len(abstract) > 30:
                return abstract
    return None
