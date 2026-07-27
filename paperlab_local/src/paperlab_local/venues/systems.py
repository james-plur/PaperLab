"""Venue adapters for the 10 system/arch/network conferences.

Parsers and fetch strategies are ported from conf-paper-collector's
fetch_conference.py and the user's parse_all.py: USENIX direct curl (no
compression), ACM via Wayback, ASPLOS/MICRO via DBLP, HPCA via researchr,
INFOCOM numbered list, SIGCOMM/SOSP accepted-page patterns.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import quote, urljoin

from ..http import fetch_text


def strip_html(raw: str) -> list[str]:
    t = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", "", t, flags=re.S | re.I)
    t = re.sub(r"</?(p|div|li|h\d|tr|td|th|br|section|article|dt|dd|span|small)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return [l.strip() for l in t.split("\n") if l.strip()]


# --- parsers -----------------------------------------------------------

_EXNAV = re.compile(
    r"^(Back to|Sign In|Conferences|Attend|Registration|Schedule|Technical Sessions|"
    r"Spring Accepted|Activities|Poster|WiP|Participate|Call for|Instructions|"
    r"Mentorship|Sponsors|Exhibitor|About|Conference|Past |Code of|Questions|"
    r"FAST|NSDI|OSDI|&#[0-9]+;|All Times|Author|Speaker|Available Media)\b", re.I)
_SKIP = re.compile(
    r"^(Keynote|Welcoming|Welcome|Award|Breakfast|Lunch|Reception|Coffee|Opening|"
    r"Closing|Photo|Panel|Plenary|Sponsor)\b", re.I)


def parse_usenix(raw: str) -> list[dict]:
    """USENIX (FAST/NSDI/OSDI): anchor on 'Available Media'."""
    ls = strip_html(raw)
    papers = []
    for i, l in enumerate(ls):
        if l.strip() == "Available Media" and i >= 2:
            title, authors = ls[i - 2], ls[i - 1]
            if _EXNAV.match(title) or _SKIP.match(title):
                continue
            if not (12 < len(title) < 300):
                continue
            papers.append({"title": title, "authors": authors})
    seen, out = set(), []
    for p in papers:
        if p["title"] not in seen:
            seen.add(p["title"])
            out.append(p)
    return out


def parse_usenix_details(raw: str) -> list[dict]:
    """Parse rich USENIX schedule paper blocks, including awards and abstracts."""
    parts = re.split(r'<article\s+id="node-\d+"\s+class="node node-paper[^\"]*"[^>]*>',
                     raw, flags=re.I)
    output = []
    seen: set[str] = set()
    for block in parts[1:]:
        title_match = re.search(
            r'<h2[^>]*>\s*<a\s+href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
            block, re.S | re.I,
        )
        if not title_match:
            continue
        href, title_raw = title_match.groups()
        title = html.unescape(re.sub(r"<[^>]+>", " ", title_raw))
        title = re.sub(r"\s+", " ", title).strip()
        key = re.sub(r"\W+", " ", title.lower()).strip()
        if not title or key in seen:
            continue
        seen.add(key)
        people_match = re.search(
            r'field-name-field-paper-people-text.*?<div class="field-item odd">\s*<p>(.*?)</p>',
            block, re.S | re.I,
        )
        people_raw = people_match.group(1) if people_match else ""
        authors = html.unescape(re.sub(r"<[^>]+>", " ", people_raw))
        authors = re.sub(r"\s+", " ", authors).strip()
        # USENIX sometimes represents award presentations and invited talks
        # with the same node-paper CSS class.  Research papers always have a
        # populated paper-people block.
        if not authors:
            continue
        organizations = []
        for org_raw in re.findall(r"<em>(.*?)</em>", people_raw, re.S | re.I):
            org = html.unescape(re.sub(r"<[^>]+>", " ", org_raw)).strip(" ;,")
            if org and org not in organizations:
                organizations.append(org)
        description_start = block.find("field-name-field-paper-description-long")
        description_end = block.find("field-name-field-paper-people", description_start + 1)
        description_raw = (block[description_start:description_end]
                           if description_start >= 0 and description_end > description_start else "")
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", description_raw, re.S | re.I)
        abstract = " ".join(
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", p))).strip()
            for p in paragraphs
        ).strip()
        awards = []
        for pattern in (r"Awarded Best Paper!", r"Distinguished Artifact Award Winner"):
            if re.search(pattern, block, re.I):
                awards.append(re.search(pattern, block, re.I).group(0))
        output.append({
            "title": title,
            "authors": authors,
            "organizations": "; ".join(organizations),
            "abstract": abstract,
            "awards": "; ".join(awards),
            "source_url": urljoin("https://www.usenix.org", href),
            "source": "usenix-official",
        })
    return output


def parse_infocom(raw: str) -> list[dict]:
    """INFOCOM numbered list: 'N. Title' then an authors line."""
    ls = strip_html(raw)
    out = []
    i = 0
    while i < len(ls):
        m = re.match(r"^(\d+)\.\s+(.+)$", ls[i])
        if m:
            title = html.unescape(m.group(2)).strip()
            authors = ls[i + 1] if i + 1 < len(ls) and not re.match(r"^\d+\.\s", ls[i + 1]) else ""
            out.append({"title": title, "authors": authors})
            i += 2
            continue
        i += 1
    return out


def parse_isca(raw: str) -> list[dict]:
    """ISCA/SOSP-like program: time-range -> title -> authors."""
    ls = strip_html(raw)
    SKIP = re.compile(
        r"^(Session|Location|Session Chair|Coffee|Break|Lunch|Breakfast|Reception|"
        r"Welcome|Keynote|Award|Photo|Panel|Closing|Opening|Plenary|Industry|Q&A|Discussion)", re.I)
    TIME = re.compile(r"^\d{1,2}:\d{2}\s*[–\u2013\u2014-]\s*\d{1,2}:\d{2}\s*(AM|PM|EDT|EST)?$", re.I)
    papers = []
    for i, l in enumerate(ls):
        if TIME.match(l) and i + 2 < len(ls):
            title, authors = ls[i + 1], ls[i + 2]
            if len(title) < 14 or title.endswith(":") or "http" in title:
                continue
            if SKIP.match(title):
                continue
            papers.append({"title": title, "authors": authors})
    seen, out = set(), []
    for p in papers:
        if p["title"] not in seen:
            seen.add(p["title"])
            out.append(p)
    return out


_SOSPNAV = re.compile(
    r"^(Home|Participate|Attend|Program|Contact|Register|Accepted Papers|The following|"
    r"TBD|Call|Instructions|Student|Volunteering|Mentoring|Women|Sponsors|Organizers|"
    r"Drop Down|Conference Schedule|Workshops|Welcome|The Event|Voting|Back to|SOSP|"
    r"Use of|This site|Clarion|September|ACM|BootstrapMade|Awards|Keynote|Congratulations|"
    r"To submit|Submissions|Notification|Camera|CFP)\b", re.I)


def parse_sosp(raw: str) -> list[dict]:
    """SOSP accepted.html: a title line followed by an author/affiliation line."""
    ls = strip_html(raw)
    papers = []
    for i in range(len(ls) - 1):
        l, nxt = ls[i], ls[i + 1]
        if _SOSPNAV.match(l) or l.startswith("#"):
            continue
        if 14 < len(l) < 220 and not l.endswith(":") and "http" not in l and "Clarion" not in l:
            looks_authors = (
                ("(" in nxt and ("," in nxt or "University" in nxt or "Lab" in nxt
                                 or ";" in nxt or "Institute" in nxt))
                or nxt.count(",") >= 1
            ) and 10 < len(nxt) < 400 and not nxt.endswith(":")
            if looks_authors:
                papers.append({"title": l, "authors": nxt})
    seen, out = set(), []
    for p in papers:
        if p["title"] not in seen:
            seen.add(p["title"])
            out.append(p)
    return out


def parse_hpca(raw: str) -> list[dict]:
    """HPCA researchr track page."""
    SKIP = re.compile(
        r"^(Break|Coffee break|Registration|Lunch|Breakfast|Welcome|Keynote|Award|"
        r"Awards|Photo|Panel|Closing|Opening|Plenary|Dinner|Excursion|Networking|Greeting|Transportation)\b", re.I)
    blocks = re.findall(
        r'<strong><a[^>]*data-event-modal="[^"]*"[^>]*>(.*?)</a></strong>(.*?)'
        r'(?=<strong><a[^>]*data-event-modal=|<div class="hidable)',
        raw, re.S)
    out, seen = [], set()
    for title_blob, rest in blocks:
        title = re.sub(r"<[^>]+>", "", title_blob)
        title = html.unescape(title).strip()
        title = re.sub(r"\s+", " ", title)
        if not title or SKIP.match(title) or not (12 < len(title) < 300):
            continue
        if title in seen:
            continue
        mtrk = re.search(r'<div class="prog-track">(.*?)</div>', rest, re.S)
        track = re.sub(r"<[^>]+>", "", mtrk.group(1)).strip() if mtrk else ""
        mperf = re.search(r'<div class="performers">(.*?)</div>', rest, re.S)
        auth = ""
        if mperf:
            a = mperf.group(1)
            a = re.sub(r'<span class="prog-aff">', " (", a)
            a = re.sub(r"</span>", ")", a)
            a = re.sub(r"<[^>]+>", "", a)
            a = html.unescape(a)
            a = re.sub(r"\s+", " ", a).strip().strip(",").strip()
            a = re.sub(r"\(\s+", "(", a)
            auth = a
        seen.add(title)
        out.append({"title": title, "authors": auth, "track": track})
    return out


def parse_dblp(raw: str) -> list[dict]:
    """DBLP volume page (ASPLOS/MICRO)."""
    parts = raw.split('<li class="entry inproceedings"')
    out = []
    for blk in parts[1:]:
        mt = re.search(r'<span class="title"[^>]*>(.*?)</span>', blk, re.S)
        if not mt:
            continue
        title = re.sub(r"<[^>]+>", " ", mt.group(1))
        title = html.unescape(title).strip()
        title = re.sub(r"\s+", " ", title)
        auths = re.findall(
            r'<span itemprop="author"[^>]*>.*?<span[^>]*itemprop="name"[^>]*>(.*?)</span>',
            blk, re.S)
        auth = ", ".join(html.unescape(re.sub(r"<[^>]+>", "", a)).strip() for a in auths)
        ee = re.search(r'<li class="ee">.*?<a[^>]+href="([^"]+)"', blk, re.S)
        doi = ""
        source_url = ee.group(1) if ee else ""
        if source_url and "doi.org/" in source_url:
            doi = source_url.split("doi.org/", 1)[1]
        out.append({"title": title, "authors": auth, "doi": doi,
                    "source_url": source_url, "source": "dblp"})
    return out


_SIGCOMM_SKIP = re.compile(
    r"^(Accepted Papers|Title|Authors|Workshops|Tutorials|N2Women|Posters|"
    r"Non-Paper|Call|Organizing|Student|Policies|Open|Visa|Local|Sponsors|SIGCOMM)\b", re.I)


def parse_sigcomm(raw: str) -> list[dict]:
    """SIGCOMM accepted page: <b>Title</b> followed by author text."""
    bs = re.findall(r"<b>(.*?)</b>", raw, re.S)
    parts = re.split(r"<b>.*?</b>", raw, flags=re.S)
    out = []
    for i, title_html in enumerate(bs):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        if not title or len(title) < 10 or _SIGCOMM_SKIP.match(title):
            continue
        auth_raw = parts[i + 1] if i + 1 < len(parts) else ""
        auth = re.sub(r"<[^>]+>", " ", auth_raw)
        auth = html.unescape(auth).replace("\n", " ").strip()
        auth = re.sub(r"\s+", " ", auth)[:600]
        out.append({"title": title, "authors": auth})
    return out


PARSERS = {
    "usenix": parse_usenix,
    "usenix_details": parse_usenix_details,
    "infocom": parse_infocom,
    "isca": parse_isca,
    "sosp": parse_sosp,
    "hpca": parse_hpca,
    "dblp": parse_dblp,
    "sigcomm": parse_sigcomm,
}


# --- fetch strategy ----------------------------------------------------

def wayback_snapshot(url: str, year: int) -> str | None:
    api = f"http://archive.org/wayback/available?url={quote(url, safe='')}&timestamp={year}0101"
    try:
        body = fetch_text(api, max_time=25)
    except RuntimeError:
        return None
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return None
    s = d.get("archived_snapshots", {}).get("closest", {})
    return s.get("url") if s.get("available") else None


CONF_CONFIG = {
    "ISCA":    {"url": "https://iscaconf.org/isca{year}/program/", "strategy": "wayback", "parser": "isca"},
    "MICRO":   {"url": "", "strategy": "dblp", "dblp": "https://dblp.org/db/conf/micro/micro{year}-{vol}.html", "parser": "dblp"},
    "HPCA":    {"url": "https://conf.researchr.org/track/hpca-{year}-main-conference", "strategy": "direct", "parser": "hpca", "compressed": True},
    "ASPLOS":  {"url": "", "strategy": "dblp", "dblp": "https://dblp.org/db/conf/asplos/asplos{year}-{vol}.html", "parser": "dblp"},
    "SIGCOMM": {"url": "https://conferences.sigcomm.org/sigcomm/{year}/accepted/", "strategy": "wayback_then_direct", "parser": "sigcomm", "compressed": True},
    "NSDI":    {"url": "https://www.usenix.org/conference/nsdi{yy}/technical-sessions", "strategy": "direct", "parser": "usenix_details", "compressed": False},
    "INFOCOM": {"url": "https://infocom{year}.ieee-infocom.org/accepted-paper-list-main-conference", "strategy": "direct", "parser": "infocom", "compressed": True},
    "FAST":    {"url": "https://www.usenix.org/conference/fast{yy}/technical-sessions", "strategy": "direct", "parser": "usenix_details", "compressed": False},
    "OSDI":    {"url": "https://www.usenix.org/conference/osdi{yy}/technical-sessions", "strategy": "direct", "parser": "usenix_details", "compressed": False},
    "SOSP":    {"url": "https://sigops.org/s/conferences/sosp/{year}/accepted.html", "strategy": "wayback_then_direct", "parser": "sosp", "compressed": True},
}

# Stable DBLP table-of-contents pages are the primary historical catalogue.
# The conference sites below remain valuable fallbacks for a newly announced
# edition that DBLP has not indexed yet.
DBLP_CONFIG = {
    "ISCA": ("isca", "isca"),
    "MICRO": ("micro", "micro"),
    "HPCA": ("hpca", "hpca"),
    "ASPLOS": ("asplos", "asplos"),
    "SIGCOMM": ("sigcomm", "sigcomm"),
    "NSDI": ("nsdi", "nsdi"),
    "INFOCOM": ("infocom", "infocom"),
    "FAST": ("fast", "fast"),
    "OSDI": ("osdi", "osdi"),
    "SOSP": ("sosp", "sosp"),
}


def _fetch_raw(url: str, compressed: bool) -> str:
    try:
        return fetch_text(url, compressed=compressed, max_time=20, retries=0)
    except RuntimeError:
        return ""


def _fetch_dblp_year(conf: str, year: int) -> list[dict]:
    """Fetch an uncapped DBLP conference-year catalogue.

    Most systems venues have one aggregate year page.  If it is absent, probe
    numbered proceedings volumes until three consecutive misses.  This also
    handles older editions whose proceedings were split by publisher volume.
    """
    slug, stem = DBLP_CONFIG[conf]
    base = f"https://dblp.org/db/conf/{slug}/{stem}{year}"
    raw = _fetch_raw(base + ".html", compressed=True)
    if raw and "entry inproceedings" in raw:
        papers = parse_dblp(raw)
        for paper in papers:
            paper["source"] = "dblp"
            paper.setdefault("source_url", base + ".html")
        return papers

    output: list[dict] = []
    seen: set[str] = set()
    misses = 0
    for volume in range(1, 65):
        url = base + f"-{volume}.html"
        raw = _fetch_raw(url, compressed=True)
        if not raw or "entry inproceedings" not in raw:
            misses += 1
            if misses >= 3:
                break
            continue
        misses = 0
        for paper in parse_dblp(raw):
            key = paper["title"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            paper["source"] = "dblp"
            paper.setdefault("source_url", url)
            output.append(paper)
    return output


def _fetch(conf: str, year: int) -> list[dict]:
    cfg = CONF_CONFIG[conf]
    yy = str(year)[2:]
    parser = PARSERS[cfg["parser"]]

    if cfg["strategy"] == "dblp":
        papers: list[dict] = []
        for vol in range(1, 6):
            url = cfg["dblp"].format(year=year, yy=yy, vol=vol)
            raw = _fetch_raw(url, compressed=True)
            if not raw or "entry inproceedings" not in raw:
                break
            papers.extend(parser(raw))
        return papers

    if cfg["strategy"] in ("wayback", "wayback_then_direct"):
        live = cfg["url"].format(year=year, yy=yy)
        snap = wayback_snapshot(live, year)
        if snap:
            raw = _fetch_raw(snap, compressed=True)
            if raw:
                return parser(raw)
        if cfg["strategy"] == "wayback_then_direct":
            raw = _fetch_raw(live, compressed=cfg.get("compressed", True))
            if raw:
                return parser(raw)
        return []

    # direct
    live = cfg["url"].format(year=year, yy=yy)
    raw = _fetch_raw(live, compressed=cfg.get("compressed", True))
    return parser(raw) if raw else []


def fetch(conf: str, year: int) -> list[dict]:
    """Return candidates for `conf`/`year` with venue/year stamped on each."""
    if conf not in CONF_CONFIG:
        raise KeyError(f"unsupported venue: {conf}")
    if conf in {"FAST", "NSDI", "OSDI"}:
        papers = _fetch(conf, year)
        if not papers:
            papers = _fetch_dblp_year(conf, year)
    else:
        papers = _fetch_dblp_year(conf, year)
        if not papers:
            papers = _fetch(conf, year)
    # Preserve DOI/source/PDF fields parsed by DBLP and official adapters.  The
    # former implementation rebuilt a tiny dict here and discarded them.
    return [{**paper, "venue": conf, "year": year} for paper in papers]
