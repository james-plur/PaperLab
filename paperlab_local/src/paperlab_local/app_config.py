"""Round-trip configuration editing for the desktop settings UI."""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from .config import load_config


def _yaml() -> YAML:
    value = YAML()
    value.preserve_quotes = True
    value.indent(mapping=2, sequence=4, offset=2)
    return value


def read_settings(path: Path) -> dict:
    parser = _yaml()
    text = path.read_text(encoding="utf-8")
    data = parser.load(text) or {}
    return {"yaml": text, "data": _plain(data), "path": str(path), "backups": list_backups(path)}


def _plain(value):
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _merge(target, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def save_settings(path: Path, payload: dict) -> dict:
    parser = _yaml()
    current_text = path.read_text(encoding="utf-8")
    if isinstance(payload.get("yaml"), str):
        candidate_text = payload["yaml"]
        parser.load(candidate_text)
    else:
        document = parser.load(current_text) or {}
        incoming = payload.get("data")
        if not isinstance(incoming, dict):
            raise ValueError("data or yaml is required")
        _merge(document, incoming)
        buffer = StringIO()
        parser.dump(document, buffer)
        candidate_text = buffer.getvalue()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml",
                                     prefix="paperlab-settings-", dir=path.parent, delete=False) as handle:
        handle.write(candidate_text)
        temp = Path(handle.name)
    try:
        load_config(temp)
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        temp.replace(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return read_settings(path)


def list_backups(path: Path) -> list[dict]:
    result = []
    for item in sorted(path.parent.glob(path.name + ".bak-*"), reverse=True)[:20]:
        result.append({"name": item.name, "path": str(item), "created_at": item.stat().st_mtime})
    return result


def restore_backup(path: Path, backup_name: str) -> dict:
    backup = path.parent / Path(backup_name).name
    if not backup.is_file() or not backup.name.startswith(path.name + ".bak-"):
        raise ValueError("invalid backup")
    content = backup.read_text(encoding="utf-8")
    _yaml().load(content)
    current = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, current)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    load_config(path)
    return read_settings(path)
