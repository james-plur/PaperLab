"""LLM helpers: keyword extraction, semantic search, and JSON chat.

Supports two backends:
- cc-switch local proxy at http://127.0.0.1:15721/v1 (Responses API wire)
- Standard OpenAI-compatible chat completions API
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any


def _get_base_url():
    env_url = os.environ.get("OPENAI_BASE_URL", "")
    if env_url:
        return env_url
    return "http://127.0.0.1:15721/v1"


def _get_api_key():
    return os.environ.get("OPENAI_API_KEY", "cc-switch")


def _get_model():
    return os.environ.get("PAPERLAB_MODEL", "zai-org/GLM-5.2")


def _client():
    from openai import OpenAI
    return OpenAI(base_url=_get_base_url(), api_key=_get_api_key())


def get_model():
    return _get_model()


def chat_json(system, user, *, model=None, retries=2, temperature=0.2):
    model = model or _get_model()
    client = _client()
    last_err = "unknown"
    use_json_mode = True
    for attempt in range(retries + 1):
        try:
            kwargs = dict(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            return _parse_json(content)
        except json.JSONDecodeError as e:
            last_err = "json: " + str(e)
        except Exception as e:
            msg = str(e)
            last_err = type(e).__name__ + ": " + msg
            if use_json_mode and (
                "response_format" in msg
                or "json" in msg.lower()
                or "400" in msg
                or "unsupported" in msg.lower()
            ):
                use_json_mode = False
                continue
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM JSON call failed (" + model + "): " + last_err)


def chat_text(system, user, *, model=None, retries=2, temperature=0.3):
    model = model or _get_model()
    client = _client()
    last_err = "unknown"
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = type(e).__name__ + ": " + str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("LLM text call failed (" + model + "): " + last_err)


# --- Keyword extraction ---

KEYWORD_SYS = (
    "You are a research paper analyzer. Given a paper title and abstract, "
    "extract 5-10 concise English keywords that capture the paper's core "
    'techniques, systems, and problem domain. Return JSON: {"keywords":["kw1","kw2",...]}. '
    "Keywords should be lowercase, specific, and useful for search matching."
)


def extract_keywords(title, abstract, *, model=None):
    """Use LLM to extract keywords from a paper title + abstract."""
    user = "Title: " + str(title) + "\nAbstract: " + str(abstract or "")[:1000]
    data = chat_json(KEYWORD_SYS, user, model=model)
    kws = data.get("keywords", [])
    return [k.strip().lower() for k in kws if k.strip()][:15]


# --- Search keyword extraction (supports Chinese input) ---

SEARCH_KW_SYS = (
    "You are a search assistant. The user gives a research interest description "
    "(may be in Chinese or English). Extract 5-8 English keywords for database search. "
    "Translate Chinese terms to their English equivalents. "
    "Include both specific terms and broader category terms. "
    'Return JSON: {"keywords":["kw1","kw2",...]}.'
)


def extract_search_keywords(description, *, model=None):
    """Extract English search keywords from a (possibly Chinese) description."""
    user = "User description: " + description
    data = chat_json(SEARCH_KW_SYS, user, model=model)
    kws = data.get("keywords", [])
    return [k.strip().lower() for k in kws if k.strip()][:10]


# --- Semantic search ---

SEARCH_SYS = (
    "You are a paper search assistant. Given a user's research interest description "
    "(may be in Chinese or English) and a list of candidate papers "
    "(id, title, keywords, abstract snippet), return the IDs of papers most "
    "relevant to the description. Judge relevance by topic overlap, technique match, "
    "and problem domain — not just keyword matching. For example, 'KV cache transfer' "
    "should match papers about KV cache disaggregation, offloading, or global caching "
    "even if the word 'transfer' is not present. "
    'Return JSON: {"matches":[{"id":"paper-id","score":0.9,"reason":"short reason in Chinese"}]}. '
    "Score 0.0-1.0. Only include papers with score >= 0.5. Max 30 results."
)


def semantic_search(description, candidates, *, model=None):
    """Match user description against candidate papers using LLM."""
    lines = ["User description: " + description, "", "Candidate papers:"]
    for c in candidates:
        kw = c.get("keywords", "") or ""
        ab = (c.get("abstract") or "")[:200]
        title = c.get("title", "") or ""
        lines.append("- id=" + str(c.get("id","")) + " | title=" + title + " | keywords=" + kw + " | abstract=" + ab)
    user = "\n".join(lines)
    data = chat_json(SEARCH_SYS, user, model=model)
    return data.get("matches", [])


def _parse_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = content.find(opener)
        end = content.rfind(closer)
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start:end + 1])
    raise json.JSONDecodeError("no JSON object found", content, 0)
