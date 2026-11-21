"""SQLite-backed authentication, session management, and RBAC service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEMO_CUSTOMER_EMAIL = "importir@nusantara.co.id"
DEMO_CUSTOMER_PASSWORD = "UserDemo2026!"
DEMO_ADMIN_EMAIL = "admin@tradegraph.id"
DEMO_ADMIN_PASSWORD = "AdminDemo2026!"


class AuthError(ValueError):
    """Raised when credentials or session tokens are invalid."""


class AuthForbiddenError(PermissionError):
    """Raised when an authenticated user lacks required role privileges."""


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: str
    email: str
    name: str
    company: str
    role: Literal["user", "admin"]
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.user_id,
            "user_id": self.user_id,
            "email": self.email,
            "name": self.name,
            "company": self.company,
            "role": self.role,
            "created_at": self.created_at,
        }


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    raw_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=raw_salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${raw_salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
    except ValueError:
        return False
    candidate = _hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, stored_hash)


class AuthService:
    """Manages users, roles (user vs admin), and session tokens in SQLite."""

    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path
        self._memory_conn: sqlite3.Connection | None = None
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_demo_accounts()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        assert self.database_path is not None
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _close_if_file(self, conn: sqlite3.Connection) -> None:
        if self._memory_conn is None:
            conn.close()

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tg_users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        company TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
                        password_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tg_sessions (
                        token TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES tg_users(user_id) ON DELETE CASCADE
                    )
                    """
                )
        finally:
            self._close_if_file(conn)

    def _seed_demo_accounts(self) -> None:
        now = datetime.now(UTC).isoformat()
        defaults = (
            (
                "usr-demo-customer",
                DEMO_CUSTOMER_EMAIL,
                "Budi Santoso",
                "PT Nusantara Logistik & Niaga",
                "user",
                DEMO_CUSTOMER_PASSWORD,
            ),
            (
                "usr-demo-admin",
                DEMO_ADMIN_EMAIL,
                "Rina Kusuma",
                "TradeGraph Regulatory & Customs Ops",
                "admin",
                DEMO_ADMIN_PASSWORD,
            ),
        )
        conn = self._connect()
        try:
            with conn:
                for uid, email, name, company, role, pwd in defaults:
                    exists = conn.execute(
                        "SELECT 1 FROM tg_users WHERE email = ?",
                        (email.lower(),),
                    ).fetchone()
                    if not exists:
                        conn.execute(
                            """
                            INSERT INTO tg_users (user_id, email, name, company, role, password_hash, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (uid, email.lower(), name, company, role, _hash_password(pwd), now),
                        )
        finally:
            self._close_if_file(conn)

    def _row_to_profile(self, row: sqlite3.Row) -> UserProfile:
        role_val: Literal["user", "admin"] = "admin" if row["role"] == "admin" else "user"
        return UserProfile(
            user_id=str(row["user_id"]),
            email=str(row["email"]),
            name=str(row["name"]),
            company=str(row["company"]),
            role=role_val,
            created_at=str(row["created_at"]),
        )

    def _issue_session(self, conn: sqlite3.Connection, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(days=7)
        conn.execute(
            """
            INSERT INTO tg_sessions (token, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, now.isoformat(), expires.isoformat()),
        )
        return token

    def register(
        self,
        *,
        name: str,
        email: str,
        password: str,
        company: str = "",
    ) -> dict[str, object]:
        clean_name = name.strip()
        clean_email = email.strip().lower()
        clean_company = company.strip() or "Perusahaan Ekspor-Impor"
        if len(clean_name) < 2:
            raise ValueError("Nama lengkap minimal 2 karakter.")
        if not _EMAIL_RE.match(clean_email):
            raise ValueError("Format alamat email tidak valid.")
        if len(password) < 6:
            raise ValueError("Kata sandi minimal 6 karakter.")

        user_id = f"usr-{secrets.token_hex(6)}"
        now = datetime.now(UTC).isoformat()
        pwd_hash = _hash_password(password)

        conn = self._connect()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT 1 FROM tg_users WHERE email = ?",
                    (clean_email,),
                ).fetchone()
                if existing:
                    raise ValueError(f"Email {clean_email} sudah terdaftar. Silakan masuk (Login).")
                conn.execute(
                    """
                    INSERT INTO tg_users (user_id, email, name, company, role, password_hash, created_at)
                    VALUES (?, ?, ?, ?, 'user', ?, ?)
                    """,
                    (user_id, clean_email, clean_name, clean_company, pwd_hash, now),
                )
                token = self._issue_session(conn, user_id)
                row = conn.execute(
                    "SELECT * FROM tg_users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                assert row is not None
                profile = self._row_to_profile(row)
        finally:
            self._close_if_file(conn)

        return {"token": token, "user": profile.to_dict()}

    def login(self, *, email: str, password: str) -> dict[str, object]:
        clean_email = email.strip().lower()
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT * FROM tg_users WHERE email = ?",
                    (clean_email,),
                ).fetchone()
                if row is None or not _verify_password(password, str(row["password_hash"])):
                    raise AuthError("Email atau kata sandi tidak sesuai.")
                profile = self._row_to_profile(row)
                token = self._issue_session(conn, profile.user_id)
        finally:
            self._close_if_file(conn)

        return {"token": token, "user": profile.to_dict()}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM tg_sessions WHERE token = ?", (token.strip(),))
        finally:
            self._close_if_file(conn)

    def get_user_by_token(self, token: str | None) -> UserProfile | None:
        if not token or not token.strip():
            return None
        now_iso = datetime.now(UTC).isoformat()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT u.*
                FROM tg_sessions s
                JOIN tg_users u ON u.user_id = s.user_id
                WHERE s.token = ? AND s.expires_at > ?
                """,
                (token.strip(), now_iso),
            ).fetchone()
            return self._row_to_profile(row) if row is not None else None
        finally:
            self._close_if_file(conn)

    def list_users(self) -> list[dict[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tg_users ORDER BY role ASC, created_at ASC"
            ).fetchall()
            return [self._row_to_profile(r).to_dict() for r in rows]
        finally:
            self._close_if_file(conn)

    def set_user_role(self, user_id: str, role: Literal["user", "admin"]) -> UserProfile:
        if role not in {"user", "admin"}:
            raise ValueError("Role harus 'user' atau 'admin'.")
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE tg_users SET role = ? WHERE user_id = ?",
                    (role, user_id.strip()),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"User {user_id} tidak ditemukan.")
                row = conn.execute(
                    "SELECT * FROM tg_users WHERE user_id = ?",
                    (user_id.strip(),),
                ).fetchone()
                assert row is not None
                return self._row_to_profile(row)
        finally:
            self._close_if_file(conn)
