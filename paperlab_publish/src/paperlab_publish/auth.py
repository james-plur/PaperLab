"""Single-admin authentication, sessions, CSRF, and device tokens."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

SESSION_COOKIE = "paperlab_session"
SESSION_TTL = 24 * 60 * 60


@dataclass
class Session:
    token: str
    csrf: str
    last_seen: float
    password_fingerprint: str


class AuthManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._hasher = PasswordHasher()

    @property
    def password_hash(self) -> str:
        return os.environ.get("PAPERLAB_ADMIN_PASSWORD_HASH", "").strip()

    def configured(self) -> bool:
        return bool(self.password_hash)

    def verify_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        try:
            return self._hasher.verify(self.password_hash, password)
        except (VerifyMismatchError, ValueError):
            return False

    def login(self, username: str, password: str, remote_ip: str) -> Session | None:
        now = time.time()
        with self._lock:
            recent = [x for x in self._failures.get(remote_ip, []) if now - x < 300]
            self._failures[remote_ip] = recent
            if len(recent) >= 10:
                return None
        expected_user = os.environ.get("PAPERLAB_ADMIN_USERNAME", "admin")
        if not hmac.compare_digest(str(username), expected_user) or not self.verify_password(password):
            with self._lock:
                self._failures.setdefault(remote_ip, []).append(now)
            return None
        fingerprint = hashlib.sha256(self.password_hash.encode()).hexdigest()
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now, fingerprint)
        with self._lock:
            self._sessions[session.token] = session
            self._failures.pop(remote_ip, None)
        return session

    def from_cookie(self, header: str | None) -> Session | None:
        cookie = SimpleCookie()
        try:
            cookie.load(header or "")
            token = cookie.get(SESSION_COOKIE).value if cookie.get(SESSION_COOKIE) else ""
        except Exception:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            fingerprint = hashlib.sha256(self.password_hash.encode()).hexdigest()
            if (not session or now - session.last_seen > SESSION_TTL or
                    session.password_fingerprint != fingerprint):
                self._sessions.pop(token, None)
                return None
            session.last_seen = now
            return session

    def logout(self, header: str | None) -> None:
        session = self.from_cookie(header)
        if session:
            with self._lock:
                self._sessions.pop(session.token, None)

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def secure_compare(value: str, expected_hash: str) -> bool:
        return hmac.compare_digest(AuthManager.token_hash(value), expected_hash)


AUTH = AuthManager()


def hash_password(password: str) -> str:
    return PasswordHasher().hash(password)
