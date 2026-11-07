"""Testes de integração — migração idempotente sobre banco com esquema ANTIGO.
Simula o upgrade na FadeHost: banco da versão antiga (tabelas mínimas) é
migrado pela nova versão. Confirma que colunas novas existem e que os dados
existentes foram preservados (sem perda de créditos, vídeos, sessões, logs).

Rodar com: python3 tests/test_integration.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

# Força o core a usar um banco temporário (não toca em data/ real).
import os
os.environ["OWNER_DISCORD_ID"] = "123"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import core.config as cfg  # noqa: E402
from core.database import get_db, migrate  # noqa: E402
from core import services  # noqa: E402


def _criar_esquema_antigo(db_path: Path) -> None:
    """Cria o banco no formato MÍNIMO da versão antiga (sem as colunas novas)."""
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            credits_cents INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            ended_at DATETIME,
            duration_hours INTEGER NOT NULL DEFAULT 24
        );
        CREATE TABLE credit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # Dados de exemplo que devem sobreviver à migração.
    con.execute("INSERT INTO users(user_id, credits_cents) VALUES (111, 500)")
    con.execute("INSERT INTO users(user_id, credits_cents) VALUES (222, 0)")
    con.execute(
        "INSERT INTO videos(user_id, filename, path) VALUES (111, 'intro.mp4', '/data/uploads/111/intro.mp4')"
    )
    con.execute(
        "INSERT INTO sessions(user_id, video_id, status, duration_hours) VALUES (111, 1, 'running', 6)"
    )
    con.execute(
        "INSERT INTO credit_logs(owner_id, user_id, amount_cents) VALUES (123, 111, 500)"
    )
    con.commit()
    con.close()


def _colunas(db_path: Path, tabela: str) -> set[str]:
    con = sqlite3.connect(str(db_path))
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({tabela})").fetchall()}
    con.close()
    return cols


def test_migra_esquema_antigo_preserva_dados(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "fadehost_antigo.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_file)
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path / "backups")

    _criar_esquema_antigo(db_file)

    # Roda a migração (idempotente: duas vezes seguidas).
    migrate()
    migrate()

    # 1) Colunas novas existem em users.
    cols_users = _colunas(db_file, "users")
    for c in ("nickname", "status", "suspended_reason", "created_at", "last_seen_at"):
        assert c in cols_users, f"coluna users.{c} faltando após migração"

    # 2) Colunas novas existem em videos.
    cols_videos = _colunas(db_file, "videos")
    for c in ("size_bytes", "mime_type", "deleted", "deleted_at", "thumbnail_path"):
        assert c in cols_videos, f"coluna videos.{c} faltando após migração"

    # 3) Colunas novas existem em sessions.
    cols_sessions = _colunas(db_file, "sessions")
    for c in (
        "price_cents", "billed_cents", "billed_millicents", "last_billed_at",
        "warned_expiry", "warned_low_credit", "downtime_seconds", "renewals",
    ):
        assert c in cols_sessions, f"coluna sessions.{c} faltando após migração"

    # 4) Colunas novas existem em credit_logs.
    cols_logs = _colunas(db_file, "credit_logs")
    for c in ("type", "reason", "balance_after", "session_id"):
        assert c in cols_logs, f"coluna credit_logs.{c} faltando após migração"

    # 5) Tabelas novas criadas.
    for t in ("durations", "admins", "tickets", "announcements", "settings", "backups", "schema_version"):
        assert t in _tabelas(db_file), f"tabela {t} faltando após migração"

    # 6) Dados antigos preservados (não houve perda).
    with get_db() as con:
        u111 = con.execute("SELECT * FROM users WHERE user_id=111").fetchone()
        assert u111 is not None, "usuário 111 desapareceu"
        assert u111["credits_cents"] == 500, f"créditos do usuário 111 alterados: {u111['credits_cents']}"
        assert u111["status"] == "active", "status default deve ser 'active'"

        vid = con.execute("SELECT * FROM videos WHERE id=1").fetchone()
        assert vid is not None and vid["filename"] == "intro.mp4"

        sess = con.execute("SELECT * FROM sessions WHERE id=1").fetchone()
        assert sess is not None and sess["status"] == "running"
        assert sess["duration_hours"] == 6

        log_row = con.execute("SELECT * FROM credit_logs WHERE id=1").fetchone()
        assert log_row is not None and log_row["amount_cents"] == 500

        # 7) Sementes de preço e settings aplicadas.
        assert len(services.list_durations(con)) == 5
        assert services.maintenance_on(con) is False
        assert services.max_sessions_per_user(con) == 2

    # 8) Backup prévio de migração foi gerado.
    backups = list((tmp_path / "backups").glob("pre_migration_*.db"))
    assert backups, "backup pré-migração não foi criado"


def _tabelas(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    tabs = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    con.close()
    return tabs


# --- Execução standalone (sem pytest) ---
if __name__ == "__main__":
    import traceback

    class MP:
        def __init__(self):
            self._saved = {}

        def setattr(self, mod, name, val):
            self._saved[(mod, name)] = getattr(mod, name)
            setattr(mod, name, val)

    falhas = 0
    with tempfile.TemporaryDirectory() as td:
        mp = MP()
        try:
            test_migra_esquema_antigo_preserva_dados(Path(td), mp)
            print("OK: migração de esquema antigo preserva dados e cria colunas/tabelas novas.")
        except Exception:
            traceback.print_exc()
            falhas += 1

    if falhas:
        print(f"FALHAS: {falhas}")
        sys.exit(1)
    print("Todos os testes de integração passaram.")
