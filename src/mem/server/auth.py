"""Device enrollment and the device registry.

The **only** server state that cannot be rebuilt from the repository
(``MEM-SERVER.md`` §3.4), which is why it lives in its own SQLite file that is
backed up alongside the repo (``MEM-RESOLUTIONS.md`` S4).

Shape is Matrix's ``client_device.yaml`` (§8): ``device_id`` is opaque, permanent
and never reused; ``display_name`` is a label the owner may edit and a remote may
never overwrite.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

from ..errors import MemError

SCHEMA = """
CREATE TABLE IF NOT EXISTS device(
  device_id    TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  token_hash   TEXT NOT NULL,
  created_at   REAL NOT NULL,
  last_seen_ip TEXT,
  last_seen_ts REAL,
  retired_at   REAL
) STRICT;
CREATE INDEX IF NOT EXISTS device_token ON device(token_hash);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(device)")}
            for name, declaration in (
                ("alias", "TEXT"), ("hostname", "TEXT"), ("os", "TEXT"), ("arch", "TEXT")
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE device ADD COLUMN {name} {declaration}")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS device_alias ON device(alias) WHERE alias IS NOT NULL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # -- enrollment --------------------------------------------------------

    def enroll(
        self, display_name: str, ip: str | None, *, alias: str, hostname: str,
        os_name: str, arch: str,
    ) -> tuple[dict, str]:
        """Mint a permanent device_id and a token, returned **once**."""
        display_name = (display_name or "").strip()
        if not display_name:
            raise MemError("invalid_request", "display_name is required")
        if len(display_name) > 64:
            raise MemError("invalid_request", "display_name is too long (max 64)")
        if (not alias or len(alias) > 40 or alias[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in alias)):
            raise MemError("invalid_request", "alias must be a lowercase slug of at most 40 characters")
        device_id = "d" + secrets.token_hex(12)
        token = secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO device(device_id, display_name, token_hash, created_at,"
                    " last_seen_ip, last_seen_ts, alias, hostname, os, arch)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (device_id, display_name, hash_token(token), now, ip, now, alias, hostname, os_name, arch),
                )
        except sqlite3.IntegrityError as exc:
            raise MemError("invalid_request", f"device alias {alias!r} is already assigned and can never be reused") from exc
        record = {
            "device_id": device_id,
            "display_name": display_name,
            "alias": alias,
            "hostname": hostname,
            "os": os_name,
            "arch": arch,
            "created_at": now,
        }
        return record, token

    # -- authentication ----------------------------------------------------

    def verify(self, token: str) -> dict | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM device WHERE token_hash = ?", (hash_token(token),)
            ).fetchone()
        if row is None or row["retired_at"] is not None:
            return None
        return dict(row)

    def touch(self, device_id: str, ip: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE device SET last_seen_ip = ?, last_seen_ts = ? WHERE device_id = ?",
                (ip, time.time(), device_id),
            )

    # -- administration (run over SSH; MEM-SERVER.md §3.3) -----------------

    def list_devices(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id, alias, display_name, hostname, os, arch, created_at, last_seen_ip, last_seen_ts,"
                " retired_at FROM device ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def alias_for(self, device_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT alias FROM device WHERE device_id = ?", (device_id,)).fetchone()
        return str(row["alias"]) if row and row["alias"] else None

    def has_alias(self, alias: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM device WHERE alias = ?", (alias,)).fetchone()
        return row is not None

    def retire(self, device_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE device SET retired_at = ? WHERE device_id = ? AND retired_at IS NULL",
                (time.time(), device_id),
            )
        return cursor.rowcount > 0
