"""Authenticated sync over a short-lived, loopback-only OpenSSH tunnel."""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests


_ACTIVE_TUNNELS = set()
_TUNNEL_LOCK = threading.Lock()


def close_sync_tunnels() -> None:
    with _TUNNEL_LOCK:
        processes = list(_ACTIVE_TUNNELS)
    for process in processes:
        _stop_tunnel(process)


atexit.register(close_sync_tunnels)


@contextmanager
def _terminate_cleanly():
    # CLI termination must unwind the context and close the SSH child.
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


@dataclass(frozen=True)
class SyncConnection:
    base_url: str
    session: requests.Session
    transport: str


def sync_configured() -> bool:
    if not os.environ.get("PAPERLAB_DEVICE_TOKEN"):
        return False
    if os.environ.get("PAPERLAB_SYNC_TRANSPORT", "ssh").lower() == "ssh":
        return bool(os.environ.get("PAPERLAB_SSH_HOST"))
    return bool(os.environ.get("PAPERLAB_SYNC_URL"))


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("PAPERLAB_SSH_REMOTE_PORT must be a port number") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PAPERLAB_SSH_REMOTE_PORT must be between 1 and 65535")
    return port


def _wait_for_tunnel(process, port: int, stderr, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr.seek(0)
            detail = stderr.read().decode("utf-8", errors="replace").strip()[-2000:]
            raise RuntimeError("SSH sync tunnel failed: " + (detail or "SSH exited"))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("SSH sync tunnel timed out; check the SSH host, key and forwarding permission")


def _stop_tunnel(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


@contextmanager
def sync_connection():
    """Never fall back to another endpoint when SSH fails."""
    transport = os.environ.get("PAPERLAB_SYNC_TRANSPORT", "ssh").lower()
    if transport not in {"ssh", "https"}:
        raise ValueError("PAPERLAB_SYNC_TRANSPORT must be ssh or https")
    with requests.Session() as session:
        if transport == "https":
            base_url = os.environ.get("PAPERLAB_SYNC_URL", "").rstrip("/")
            url = urlsplit(base_url)
            if url.username or url.password or url.query or url.fragment or not url.hostname:
                raise ValueError("PAPERLAB_SYNC_URL must be an HTTP(S) URL without credentials or query")
            if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError("Direct sync requires HTTPS; HTTP is only permitted on loopback")
            yield SyncConnection(base_url, session, transport)
            return
        host = os.environ.get("PAPERLAB_SSH_HOST", "")
        if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", host):
            raise ValueError("Set PAPERLAB_SSH_HOST to a host or alias from ~/.ssh/config")
        remote_port = _port(os.environ.get("PAPERLAB_SSH_REMOTE_PORT", "8765"))
        ssh = shutil.which("ssh")
        if not ssh:
            raise RuntimeError("SSH sync requires the OpenSSH ssh command")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            local_port = reservation.getsockname()[1]
        known_hosts = Path(os.environ.get("PAPERLAB_SSH_KNOWN_HOSTS", "~/.ssh/known_hosts")).expanduser()
        if str(known_hosts) == "/dev/null":
            raise ValueError("SSH sync requires a persistent known_hosts file")
        command = [ssh, "-N", "-T", "-S", "none",
                   "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"UserKnownHostsFile={known_hosts}",
                   "-o", "ForkAfterAuthentication=no", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=2", "-L",
                   f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", host]
        # Ignore HTTP proxy and netrc settings for this authenticated SSH connection.
        session.trust_env = False
        with _terminate_cleanly(), tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=stderr)
            with _TUNNEL_LOCK:
                _ACTIVE_TUNNELS.add(process)
            try:
                _wait_for_tunnel(process, local_port, stderr)
                yield SyncConnection(f"http://127.0.0.1:{local_port}", session, transport)
            finally:
                _stop_tunnel(process)
                with _TUNNEL_LOCK:
                    _ACTIVE_TUNNELS.discard(process)
