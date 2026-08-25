"""OpenAI-compatible LLM providers with ordered failover."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


PROVIDER_DEFAULTS = {
    "siliconflow": {
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "",
        "api_key_env": "SILICONFLOW_API_KEY",
        "enabled": True,
        "timeout": 90,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "enabled": True,
        "timeout": 90,
    },
    "zhipu": {
        "label": "Zhipu GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "model": "GLM-4.7-Flash",
        "api_key_env": "ZAI_API_KEY",
        "enabled": True,
        "timeout": 90,
    },
    "cc_switch": {
        "label": "CC Switch",
        "base_url": "http://127.0.0.1:15721/v1",
        "model": "zai-org/GLM-5.2",
        "api_key_env": "OPENAI_API_KEY",
        "enabled": False,
        "timeout": 90,
    },
}
DEFAULT_PRIORITY = ["siliconflow", "deepseek", "zhipu"]


def config_path(db_path=None) -> Path:
    override = os.environ.get("PAPERLAB_LLM_CONFIG")
    if override:
        return Path(override).expanduser()
    if db_path:
        return Path(db_path).expanduser().resolve().parent / "llm_config.json"
    return Path(__file__).resolve().parent.parent / "data" / "llm_config.json"


def load_provider_config(db_path=None) -> dict:
    path = config_path(db_path)
    saved = {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    if not isinstance(saved, dict):
        saved = {}
    providers = {}
    saved_providers = saved.get("providers")
    if not isinstance(saved_providers, dict):
        saved_providers = {}
    for name, defaults in PROVIDER_DEFAULTS.items():
        values = dict(defaults)
        saved_name = "codex" if name == "cc_switch" and "cc_switch" not in saved_providers else name
        if isinstance(saved_providers, dict) and isinstance(saved_providers.get(saved_name), dict):
            values.update({k: v for k, v in saved_providers[saved_name].items()
                           if k in {"base_url", "model", "api_key", "enabled", "timeout"}})
        providers[name] = values
    active = saved.get("active_provider", "zhipu")
    if active not in providers:
        active = "zhipu"
    priority = [name for name in saved.get("priority", DEFAULT_PRIORITY) if name in providers]
    priority.extend(name for name in DEFAULT_PRIORITY if name not in priority)
    return {"active_provider": active, "priority": priority, "providers": providers}


def public_provider_config(db_path=None) -> dict:
    cfg = load_provider_config(db_path)
    public = {"active_provider": cfg["active_provider"], "priority": cfg["priority"], "providers": {}}
    for name, values in cfg["providers"].items():
        env_key = os.environ.get(values["api_key_env"], "")
        public["providers"][name] = {
            "label": values["label"],
            "base_url": values["base_url"],
            "model": values["model"],
            "enabled": bool(values.get("enabled", True)),
            "timeout": int(values.get("timeout", 90)),
            "has_api_key": bool(values.get("api_key") or env_key or
                                (name == "zhipu" and os.environ.get("ZHIPU_API_KEY")) or
                                name == "cc_switch"),
        }
    return public


def save_provider_config(db_path, payload: dict) -> dict:
    current = load_provider_config(db_path)
    active = str(payload.get("active_provider") or current["active_provider"])
    if active not in PROVIDER_DEFAULTS:
        raise ValueError("unsupported LLM provider: " + active)
    incoming = payload.get("providers") or {}
    priority = [str(name) for name in payload.get("priority", current["priority"])
                if str(name) in PROVIDER_DEFAULTS]
    priority.extend(name for name in DEFAULT_PRIORITY if name not in priority)
    stored = {"schema_version": 3, "active_provider": active, "priority": priority, "providers": {}}
    for name in PROVIDER_DEFAULTS:
        values = current["providers"][name]
        update = incoming.get(name) if isinstance(incoming, dict) else None
        if isinstance(update, dict):
            for field in ("base_url", "model"):
                if field in update:
                    value = str(update[field]).strip()
                    if not value and field == "base_url":
                        raise ValueError(f"{name}.{field} cannot be empty")
                    values[field] = value
            if "enabled" in update:
                values["enabled"] = bool(update["enabled"])
            if "timeout" in update:
                values["timeout"] = max(5, min(300, int(update["timeout"])))
        stored["providers"][name] = {
            key: values[key] for key in ("base_url", "model", "enabled", "timeout")
            if values.get(key) is not None
        }
    path = config_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    return public_provider_config(db_path)


def _provider(provider=None, db_path=None, model=None):
    cfg = load_provider_config(db_path)
    name = provider or cfg["active_provider"]
    if name not in cfg["providers"]:
        raise ValueError("unsupported LLM provider: " + str(name))
    values = dict(cfg["providers"][name])
    values["name"] = name
    if model:
        values["model"] = model
    if not values.get("enabled", True):
        raise RuntimeError(f"{values['label']} is disabled")
    if name == "cc_switch":
        values["base_url"] = os.environ.get("OPENAI_BASE_URL", values["base_url"])
        values["model"] = os.environ.get("PAPERLAB_MODEL", values["model"])
    elif name == "zhipu":
        values["base_url"] = os.environ.get("ZAI_BASE_URL", values["base_url"])
        values["model"] = os.environ.get("ZAI_MODEL", values["model"])
    values["api_key"] = (values.get("api_key") or
                         os.environ.get(values["api_key_env"], "") or
                         (os.environ.get("ZHIPU_API_KEY", "") if name == "zhipu" else "") or
                         ("cc-switch" if name == "cc_switch" else ""))
    if not values["api_key"]:
        raise RuntimeError(f"{values['label']} API key is not configured")
    return values


def _get_base_url():
    env_url = os.environ.get("ZAI_BASE_URL", "")
    if env_url:
        return env_url
    return "https://open.bigmodel.cn/api/paas/v4/"


def _get_api_key():
    return os.environ.get("ZAI_API_KEY", "")


def _get_model():
    return os.environ.get("ZAI_MODEL", "glm-5.2")


def _client(*, provider=None, db_path=None, model=None):
    from openai import OpenAI
    selected = _provider(provider, db_path, model)
    return OpenAI(base_url=selected["base_url"], api_key=selected["api_key"],
                  timeout=float(selected.get("timeout", 90))), selected


def list_models(provider: str, db_path=None) -> list[str]:
    client, _selected = _client(provider=provider, db_path=db_path)
    return sorted({str(item.id) for item in client.models.list().data if getattr(item, "id", None)})


def test_provider(provider: str, db_path=None) -> dict:
    started = time.monotonic()
    selected = _provider(provider, db_path)
    if not selected.get("model"):
        raise RuntimeError("model is not configured")
    answer = chat_text("Reply with OK only.", "Connection test", provider=provider,
                       model=selected["model"], db_path=db_path, retries=0, temperature=0)
    return {"status": "ok", "provider": provider, "model": selected["model"],
            "latency_ms": round((time.monotonic() - started) * 1000), "response": answer[:40]}


def chat_text_fallback(system, user, *, db_path=None, history=None, temperature=0.3):
    cfg = load_provider_config(db_path)
    failures = []
    for name in cfg["priority"]:
        values = cfg["providers"][name]
        if not values.get("enabled", True) or not values.get("model"):
            continue
        try:
            answer = chat_text(system, user, provider=name, model=values["model"],
                               db_path=db_path, retries=0, temperature=temperature)
            return {"answer": answer, "provider": name, "model": values["model"],
                    "fallback": bool(failures)}
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    raise RuntimeError("all configured LLM providers failed: " + "; ".join(failures or ["none configured"]))


def get_model(provider=None, db_path=None):
    cfg = load_provider_config(db_path)
    name = provider or cfg["active_provider"]
    if name not in cfg["providers"]:
        raise ValueError("unsupported LLM provider: " + str(name))
    model = cfg["providers"][name]["model"]
    if name == "cc_switch":
        return os.environ.get("PAPERLAB_MODEL", model)
    return os.environ.get("ZAI_MODEL", model) if name == "zhipu" else model


def chat_json(system, user, *, model=None, provider=None, db_path=None,
              retries=2, temperature=0.2):
    client, selected = _client(provider=provider, db_path=db_path, model=model)
    model = selected["model"]
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


def chat_text(system, user, *, model=None, provider=None, db_path=None,
              retries=2, temperature=0.3):
    client, selected = _client(provider=provider, db_path=db_path, model=model)
    model = selected["model"]
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
