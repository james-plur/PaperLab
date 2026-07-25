"""Normalize organization names for merging and filtering.

Organizations from different sources (OpenAlex, Crossref, DBLP) have many variant
spellings and combined forms (e.g. "Alibaba Cloud and Tsinghua University").
This module splits combined orgs, normalizes spelling, and maps known variants
to canonical names so the filter dropdown stays manageable.
"""
from __future__ import annotations

import re

# Canonical name -> set of lowercase patterns that should map to it
_CANONICAL_MAP: dict[str, list[str]] = {
    "Tsinghua University": ["tsinghua"],
    "Peking University": ["peking university", "peking univ", "pku"],
    "Shanghai Jiao Tong University": ["shanghai jiao tong", "sjtu"],
    "Zhejiang University": ["zhejiang university", "zju"],
    "Fudan University": ["fudan university"],
    "Nanjing University": ["nanjing university"],
    "University of Science and Technology of China": ["ustc", "university of science and technology of china"],
    "Huazhong University of Science and Technology": ["huazhong university of science and technology", "hust"],
    "Beihang University": ["beihang"],
    "Beijing University of Posts and Telecommunications": ["beijing university of posts and telecommunications", "bupt"],
    "Wuhan University": ["wuhan university"],
    "Harbin Institute of Technology": ["harbin institute of technology", "hit"],
    "Sun Yat-sen University": ["sun yat-sen", "sun yat sen"],
    "South China University of Technology": ["south china university of technology", "scut"],
    "Xidian University": ["xidian"],
    "Xiamen University": ["xiamen university"],
    "Sichuan University": ["sichuan university"],
    "Tongji University": ["tongji university"],
    "Renmin University of China": ["renmin university of china", "ruc"],
    "Nankai University": ["nankai university"],
    "Tianjin University": ["tianjin university"],
    "Dalian University of Technology": ["dalian university of technology"],
    "Northeastern University": ["northeastern university"],
    "University of Chinese Academy of Sciences": ["university of chinese academy of sciences", "ucas"],
    "Institute of Computing Technology CAS": ["institute of computing technology", "ict cas"],
    "Chinese Academy of Sciences": ["chinese academy of sciences", "cas"],
    "National University of Defense Technology": ["national university of defense technology", "nudt"],
    "Shandong University": ["shandong university"],
    "Chongqing University": ["chongqing university"],
    "Jilin University": ["jilin university"],
    "Lanzhou University": ["lanzho university"],
    "Northwestern Polytechnical University": ["northwestern polytechnical"],
    "Chinese University of Hong Kong": ["chinese university of hong kong", "cuhk"],
    "Hong Kong University of Science and Technology": ["hong kong university of science and technology", "hkust"],
    "University of Hong Kong": ["university of hong kong", "hku"],
    "Hong Kong Polytechnic University": ["hong kong polytechnic", "polyu"],
    "City University of Hong Kong": ["city university of hong kong", "cityu"],
    "Hong Kong Baptist University": ["hong kong baptist"],
    "National Taiwan University": ["national taiwan university", "ntu"],
    "Tsing Hua University Taiwan": ["national tsing hua university taiwan", "nthu"],
    "KAIST": ["kaist", "korea advanced institute"],
    "Seoul National University": ["seoul national university"],
    "Korea University": ["korea university"],
    "Yonsei University": ["yonsei university"],
    "POSTECH": ["postech", "pohang university"],
    "Seoul National University of Science and Technology": ["seoul national university of science"],
    "Hanyang University": ["hanyang university"],
    "Sungkyunkwan University": ["sungkyunkwan"],
    "INHA University": ["inha university"],
    "University of Tokyo": ["university of tokyo", "utokyo"],
    "Osaka University": ["osaka university"],
    "Kyoto University": ["kyoto university"],
    "Tokyo Institute of Technology": ["tokyo institute of technology"],
    "Tohoku University": ["tohoku university"],
    "Nagoya University": ["nagoya university"],
    "NTU Singapore": ["nanyang technological university", "ntu singapore"],
    "National University of Singapore": ["national university of singapore", "nus"],
    "Singapore University of Technology and Design": ["singapore university of technology"],
    "IIT Bombay": ["iit bombay", "indian institute of technology bombay"],
    "IIT Delhi": ["iit delhi"],
    "IIT Madras": ["iit madras"],
    "IISc Bangalore": ["indian institute of science", "iisc"],
    "IIT Kanpur": ["iit kanpur"],
    "IIT Kharagpur": ["iit kharagpur"],
    "University of Melbourne": ["university of melbourne"],
    "University of Sydney": ["university of sydney"],
    "UNSW Sydney": ["university of new south wales", "unsw"],
    "University of Queensland": ["university of queensland"],
    "Monash University": ["monash university"],
    "MIT": ["massachusetts institute of technology", "mit "],
    "Stanford University": ["stanford university", "stanford"],
    "UC Berkeley": ["university of california berkeley", "uc berkeley", "berkeley"],
    "Carnegie Mellon University": ["carnegie mellon", "cmu"],
    "Georgia Tech": ["georgia institute of technology", "georgia tech"],
    "Princeton University": ["princeton university"],
    "Cornell University": ["cornell university"],
    "University of Michigan": ["university of michigan"],
    "University of Washington": ["university of washington"],
    "University of Illinois Urbana-Champaign": ["university of illinois urbana", "uiuc"],
    "University of Wisconsin-Madison": ["university of wisconsin"],
    "University of Texas at Austin": ["university of texas at austin", "ut austin"],
    "Purdue University": ["purdue university"],
    "Yale University": ["yale university"],
    "Duke University": ["duke university"],
    "Columbia University": ["columbia university"],
    "Harvard University": ["harvard university"],
    "University of Pennsylvania": ["university of pennsylvania", "upenn"],
    "University of Southern California": ["university of southern california", "usc"],
    "New York University": ["new york university", "nyu"],
    "Ohio State University": ["ohio state university"],
    "Penn State University": ["penn state", "pennsylvania state university"],
    "Arizona State University": ["arizona state university", "asu"],
    "Rice University": ["rice university"],
    "Boston University": ["boston university"],
    "Northeastern University USA": ["northeastern university boston"],
    "University of Chicago": ["university of chicago"],
    "Johns Hopkins University": ["johns hopkins"],
    "Northwestern University": ["northwestern university"],
    "University of Virginia": ["university of virginia"],
    "Virginia Tech": ["virginia tech", "virginia polytechnic"],
    "University of Maryland": ["university of maryland"],
    "Syracuse University": ["syracuse university"],
    "Florida International University": ["florida international"],
    "Cambridge University": ["university of cambridge", "cambridge university"],
    "Oxford University": ["university of oxford", "oxford university"],
    "Imperial College London": ["imperial college"],
    "ETH Zurich": ["eth zurich", "ethz"],
    "EPFL": ["ecole polytechnique federale de lausanne", "epfl"],
    "KTH Royal Institute of Technology": ["kth royal", "kth "],
    "Sorbonne University": ["sorbonne"],
    "INRIA": ["inria"],
    "Max Planck Institute": ["max planck"],
    "TU Munich": ["technical university of munich", "tum ", "tu munich"],
    "TU Delft": ["delft university of technology", "tu delft"],
    "University of Edinburgh": ["university of edinburgh"],
    "UCL London": ["university college london", "ucl "],
    "University of St Andrews": ["university of st andrews", "st andrews"],
    "Alibaba": ["alibaba"],
    "Tencent": ["tencent"],
    "ByteDance": ["bytedance"],
    "Baidu": ["baidu"],
    "Huawei": ["huawei"],
    "Microsoft Research": ["microsoft research", "msr "],
    "Microsoft": ["microsoft"],
    "Google": ["google"],
    "Google DeepMind": ["deepmind"],
    "Meta": ["meta ", "facebook ai", "facebook "],
    "Amazon": ["amazon"],
    "Apple": ["apple"],
    "NVIDIA": ["nvidia"],
    "Intel": ["intel"],
    "AMD": ["amd", "advanced micro devices"],
    "Samsung": ["samsung"],
    "Hugging Face": ["hugging face", "huggingface"],
    "Moonshot AI": ["moonshot ai", "moonshot"],
    "OpenAI": ["openai"],
    "Anthropic": ["anthropic"],
    "Salesforce": ["salesforce"],
    "IBM Research": ["ibm research"],
    "IBM": ["ibm "],
    "Oracle": ["oracle"],
    "Cisco": ["cisco"],
    "VMware": ["vmware"],
    "Qualcomm": ["qualcomm"],
    "Arm": [" arm ", "arm ltd", "arm limited"],
}

# Build reverse lookup: lowercase pattern -> canonical name
_PATTERN_TO_CANON: dict[str, str] = {}
for canon, patterns in _CANONICAL_MAP.items():
    for p in patterns:
        _PATTERN_TO_CANON[p] = canon

# Common junk patterns to strip
_JUNK_RE = re.compile(r"\s*\(.*?\)", re.S)  # parenthetical notes like (China)
_SPLIT_RE = re.compile(r"\s*(?:;|,| and | & |/|\|)\s*", re.I)


def _clean_name(name: str) -> str:
    """Strip parenthetical notes, extra whitespace, and HTML entities."""
    name = _JUNK_RE.sub("", name)
    name = name.replace("&amp;", "&").replace("，", ",")
    name = re.sub(r"\s+", " ", name).strip()
    # Fix common typo
    name = name.replace("Tsinghua Univesity", "Tsinghua University")
    # Capitalize first letter
    if name:
        name = name[0].upper() + name[1:]
    return name


def normalize_org(name: str) -> str | None:
    """Map a raw org string to its canonical name, or return the cleaned string.

    Returns ``None`` for empty/junk strings.
    """
    name = _clean_name(name)
    if not name or len(name) < 3:
        return None
    lower = name.lower()
    # Try exact match first
    if lower in _PATTERN_TO_CANON:
        return _PATTERN_TO_CANON[lower]
    # Try substring match (longest pattern first for specificity)
    best_match = None
    best_len = 0
    for pattern, canon in _PATTERN_TO_CANON.items():
        if pattern in lower and len(pattern) > best_len:
            best_match = canon
            best_len = len(pattern)
    if best_match:
        return best_match
    return name


def split_and_normalize(org_string: str) -> list[str]:
    """Split a combined org string and normalize each part.

    Handles strings like:
      "Tsinghua University and Alibaba Cloud"
      "Google; Stanford University"
      "MIT / Google Research"
    """
    parts = _SPLIT_RE.split(org_string)
    results: list[str] = []
    seen = set()
    for part in parts:
        norm = normalize_org(part)
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            results.append(norm)
    return results
