"""Camada de banco de dados SQLite + migrações idempotentes.

REGRAS OBRIGATÓRIAS:
- Nenhuma migração destrutiva (sem DROP TABLE / DROP COLUMN).
- Antes de aplicar migrações, faz backup seguro via sqlite3 Connection.backup().
- Toda coluna nova é adicionada com ALTER TABLE ... ADD COLUMN guardado por PRAGMA.
- O banco real com dados dos usuários vive na FadeHost; este módulo roda lá na primeira
  inicialização da nova versão e preserva todos os dados existentes.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_db():
    """Conexão com row_factory, WAL e foreign keys ligadas. Comita no sucesso."""
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def backup_db(dest: Path) -> Path:
    """Backup seguro usando a API online do SQLite (não copia arquivo em uso)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return r is not None


def _add_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if not _column_exists(con, table, column):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def migrate() -> None:
    """Aplica migrações de forma idempotente. Faz backup prévio se o banco já existir."""
    if config.DB_PATH.exists() and config.DB_PATH.stat().st_size > 0:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        try:
            backup_db(config.BACKUP_DIR / f"pre_migration_{ts}.db")
        except Exception as exc:  # não bloqueia a migração, mas loga
            print(f"[migrate] AVISO: backup prévio falhou: {exc}")

    with get_db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )

        # ---- v1: tabelas originais (preservam dados existentes) ----
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                credits_cents INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME,
                ended_at DATETIME,
                duration_hours INTEGER NOT NULL DEFAULT 24
            )
            """
        )
        # Colunas originais adicionadas por migração ad-hoc no bot antigo.
        _add_column(con, "sessions", "expires_at", "expires_at DATETIME")
        _add_column(con, "sessions", "duration_hours", "duration_hours INTEGER DEFAULT 24")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS credit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # ---- v2: colunas novas (não destrutivo) ----
        _add_column(con, "users", "nickname", "nickname TEXT")
        _add_column(con, "users", "status", "status TEXT NOT NULL DEFAULT 'active'")
        _add_column(con, "users", "suspended_reason", "suspended_reason TEXT")
        # NOTA: SQLite (incluindo versões antigas da FadeHost) recusa ALTER TABLE
        # ADD COLUMN com default não-constante (CURRENT_TIMESTAMP) em tabelas que
        # já têm linhas. Por isso criamos sem default e fazemos backfill via UPDATE.
        _add_column(con, "users", "created_at", "created_at DATETIME")
        con.execute(
            "UPDATE users SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        _add_column(con, "users", "last_seen_at", "last_seen_at DATETIME")

        _add_column(con, "videos", "size_bytes", "size_bytes INTEGER DEFAULT 0")
        _add_column(con, "videos", "mime_type", "mime_type TEXT")
        _add_column(con, "videos", "deleted", "deleted INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "videos", "deleted_at", "deleted_at DATETIME")
        _add_column(con, "videos", "thumbnail_path", "thumbnail_path TEXT")

        _add_column(con, "sessions", "price_cents", "price_cents INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "billed_cents", "billed_cents INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "billed_millicents", "billed_millicents INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "last_billed_at", "last_billed_at DATETIME")
        _add_column(con, "sessions", "warned_expiry", "warned_expiry INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "warned_low_credit", "warned_low_credit INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "downtime_seconds", "downtime_seconds INTEGER NOT NULL DEFAULT 0")
        _add_column(con, "sessions", "renewals", "renewals INTEGER NOT NULL DEFAULT 0")

        _add_column(con, "credit_logs", "type", "type TEXT NOT NULL DEFAULT 'manual'")
        _add_column(con, "credit_logs", "reason", "reason TEXT")
        _add_column(con, "credit_logs", "balance_after", "balance_after INTEGER")
        _add_column(con, "credit_logs", "session_id", "session_id INTEGER")

        # ---- v3: tabelas novas ----
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS durations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hours INTEGER NOT NULL UNIQUE,
                price_cents INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'open',
                subject TEXT,
                opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_activity_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                closed_at DATETIME,
                closed_by INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'ok'
            )
            """
        )

        # ---- v4: sementes e consistência ----
        n = con.execute("SELECT COUNT(*) AS c FROM durations").fetchone()["c"]
        if n == 0:
            for hours, price in config.SEED_PRICES.items():
                con.execute(
                    "INSERT OR IGNORE INTO durations(hours, price_cents) VALUES (?, ?)",
                    (hours, price),
                )

        defaults = {
            "max_sessions_per_user": str(config.DEFAULT_MAX_SESSIONS_PER_USER),
            "max_upload_mb": str(config.DEFAULT_MAX_UPLOAD_MB),
            "video_retention_hours": str(config.DEFAULT_VIDEO_RETENTION_HOURS),
            "ticket_auto_close_hours": str(config.DEFAULT_TICKET_AUTO_CLOSE_HOURS),
            "backup_retention_days": str(config.DEFAULT_BACKUP_RETENTION_DAYS),
            "maintenance_mode": "0",
            "maintenance_message": "Manutenção em andamento. Novas sessões e uploads estão temporariamente bloqueados.",
            "suspended_session_policy": "allow_until_expire",  # ou 'end_immediately'
            "last_heartbeat": _now_iso(),
        }
        for key, val in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, val)
            )

        # Backfill: sessões antigas sem preço registrado -> preço da duração, se existir.
        con.execute(
            """
            UPDATE sessions
            SET price_cents = COALESCE(
                (SELECT price_cents FROM durations WHERE hours = sessions.duration_hours),
                0
            )
            WHERE price_cents = 0
            """
        )
        # Sessões antigas 'running' com último heartbeat antigo serão recuperadas pelo
        # cog de tarefas em background (não aqui, para não cobrar tempo de downtime).

        con.execute("DELETE FROM schema_version")
        con.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))


# ---------- Helpers de settings ----------
def get_setting(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, value),
    )


def get_int_setting(con: sqlite3.Connection, key: str, default: int) -> int:
    v = get_setting(con, key, None)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def ensure_user(con: sqlite3.Connection, user_id: int) -> None:
    con.execute(
        "INSERT OR IGNORE INTO users(user_id, created_at) VALUES (?, CURRENT_TIMESTAMP)",
        (user_id,),
    )
    con.execute(
        "UPDATE users SET last_seen_at=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,)
    )
