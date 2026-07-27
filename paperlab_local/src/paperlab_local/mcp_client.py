"""Small stdio client for the installed openags/paper-search-mcp server."""
from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class MCPError(RuntimeError):
    pass


class PaperSearchMCP:
    """Synchronous JSON-RPC wrapper around paper-search-mcp's stdio server.

    Keeping the integration at the MCP boundary means PaperLab uses the exact
    same tools that are available to Codex, while still working from launchd.
    """

    def __init__(self, command: str, args: list[str], *, timeout: int = 120):
        self.command = command
        self.args = args
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        response = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "paperlab_local", "version": "0.2.0"},
        })
        if not response:
            raise MCPError("paper-search-mcp returned an empty initialize response")
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def _write(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("paper-search-mcp is not running")
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int) -> dict:
        if self._proc is None or self._proc.stdout is None:
            raise MCPError("paper-search-mcp is not running")
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"paper-search-mcp timed out after {self.timeout}s")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                raise MCPError(f"paper-search-mcp timed out after {self.timeout}s")
            line = self._proc.stdout.readline()
            if not line:
                code = self._proc.poll()
                raise MCPError(f"paper-search-mcp closed stdout (exit={code})")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    raise MCPError(str(message["error"]))
                return message.get("result") or {}

    def _request(self, method: str, params: dict) -> dict:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return self._read_response(request_id)

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    @staticmethod
    def _unwrap(result: dict) -> Any:
        structured = result.get("structuredContent") or {}
        if "result" in structured:
            return structured["result"]
        for item in result.get("content") or []:
            if item.get("type") != "text":
                continue
            text = item.get("text", "")
            try:
                parsed = json.loads(text)
                return parsed.get("result", parsed) if isinstance(parsed, dict) else parsed
            except json.JSONDecodeError:
                return text
        return result

    def call(self, name: str, arguments: dict) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(str(self._unwrap(result)))
        return self._unwrap(result)

    def search(self, query: str, *, sources: list[str], year: str | None = None,
               max_results_per_source: int = 3) -> dict:
        args: dict[str, Any] = {
            "query": query,
            "sources": ",".join(sources),
            "max_results_per_source": max_results_per_source,
        }
        if year:
            args["year"] = year
        result = self.call("search_papers", args)
        return result if isinstance(result, dict) else {"papers": []}

    def download_with_fallback(self, *, source: str, paper_id: str,
                               save_path: Path, doi: str = "", title: str = "",
                               use_scihub: bool = False,
                               scihub_base_url: str = "") -> str:
        args: dict[str, Any] = {
            "source": source, "paper_id": paper_id, "doi": doi, "title": title,
            "save_path": str(save_path), "use_scihub": use_scihub,
        }
        if scihub_base_url:
            args["scihub_base_url"] = scihub_base_url
        result = self.call("download_with_fallback", args)
        return str(result or "")
