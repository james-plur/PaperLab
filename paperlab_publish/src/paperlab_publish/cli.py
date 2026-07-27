"""Minimal command line for the independently deployed Publish service."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from .config import load_config
from .web import serve


def _load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperlab-publish")
    parser.add_argument("--config", "-c", default="topics.yaml")
    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="run the public web and sync service")
    web.add_argument("action", nargs="?", default="serve", choices=("serve",))
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)

    auth = sub.add_parser("auth", help="authentication utilities")
    auth_sub = auth.add_subparsers(dest="auth_action", required=True)
    auth_sub.add_parser("hash-password")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    _load_env(config_path.parent / ".env")
    if args.command == "auth":
        import getpass
        from .auth import hash_password
        first = getpass.getpass("Admin password: ")
        second = getpass.getpass("Confirm password: ")
        if not first or first != second:
            raise SystemExit("passwords do not match or are empty")
        print(hash_password(first))
        return 0
    cfg = load_config(config_path)
    serve(cfg.db_path, host=getattr(args, "host", "127.0.0.1"),
          port=getattr(args, "port", 8765), config_path=config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
