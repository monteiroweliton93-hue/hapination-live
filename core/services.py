"""Funções puras de negócio e leituras compartilhadas (contrato entre cogs).

Tudo aqui é síncrono e opera sobre uma conexão SQLite aberta ou valores puros.
Cogs de painel/admin leem dados através destas funções (e dos métodos expostos
por cada cog via bot.get_cog) — NÃO duplique SQL de leitura em vários lugares.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import config
from .database import ensure_user, get_int_setting, get_setting

# ---------------- Preços / durações ----------------
def list_durations(con: sqlite3.Connection, only_active: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM durations"
    if only_active:
        q += " WHERE active=1"
    q += " ORDER BY hours ASC"
    return con.execute(q).fetchall()


def price_for_hours(con: sqlite3.Connection, hours: int) -> int | None:
    row = con.execute(
        "SELECT price_cents FROM durations WHERE hours=? AND active=1", (hours,)
    ).fetchone()
    return row["price_cents"] if row else None


# ---------------- Créditos ----------------
def get_balance(con: sqlite3.Connection, user_id: int) -> int:
    ensure_user(con, user_id)
    row = con.execute(
        "SELECT credits_cents FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    return row["credits_cents"]


def adjust_credits(
    con: sqlite3.Connection,
    user_id: int,
    delta_cents: int,
    *,
    actor_id: int,
    log_type: str,
    reason: str | None = None,
    session_id: int | None = None,
) -> int:
    """Ajusta saldo (delta pode ser negativo) e registra movimentação. Retorna novo saldo.

    NÃO chame isto para cobrança de sessão — use a função de cobrança do SessionsCog,
    que garante cobrança proporcional e sem duplicatas. Aqui é para add/remove/correção
    manuais (SOMENTE proprietário) e ajustes pontuais.
    """
    ensure_user(con, user_id)
    con.execute(
        "UPDATE users SET credits_cents = credits_cents + ? WHERE user_id=?",
        (delta_cents, user_id),
    )
    new_balance = get_balance(con, user_id)
    con.execute(
        """
        INSERT INTO credit_logs(owner_id, user_id, amount_cents, type, reason,
                                balance_after, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor_id, user_id, delta_cents, log_type, reason, new_balance, session_id),
    )
    return new_balance


# ---------------- Sessões (leituras) ----------------
def active_sessions(con: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT * FROM sessions
        WHERE user_id=? AND status='running'
        ORDER BY started_at DESC
        """,
        (user_id,),
    ).fetchall()


def active_sessions_count(con: sqlite3.Connection, user_id: int) -> int:
    row = con.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND status='running'",
        (user_id,),
    ).fetchone()
    return row["c"]


def max_sessions_per_user(con: sqlite3.Connection) -> int:
    return get_int_setting(con, "max_sessions_per_user", config.DEFAULT_MAX_SESSIONS_PER_USER)


# ---------------- Cobrança proporcional (matemática pura) ----------------
def minute_cost_millicents(price_cents: int, total_minutes: int) -> int:
    """Custo por minuto em milésimos de centavo (inteiro, sem erro de ponto flutuante)."""
    if total_minutes <= 0:
        return 0
    return (price_cents * 1000 + total_minutes - 1) // total_minutes


def billing_step(
    price_cents: int,
    total_minutes: int,
    billed_millicents: int,
    billed_cents: int,
    minutes_elapsed: int,
) -> tuple[int, int, int]:
    """Um passo de cobrança por minuto.

    Retorna (novo_billed_millicents, novo_billed_cents, centavos_a_descontar_agora).
    Teto: nunca cobra além de price_cents.
    """
    if billed_cents >= price_cents:
        return billed_millicents, billed_cents, 0
    per_min = minute_cost_millicents(price_cents, total_minutes)
    new_milli = billed_millicents + per_min * max(0, minutes_elapsed)
    chargeable = new_milli // 1000
    new_milli %= 1000
    chargeable = min(chargeable, price_cents - billed_cents)
    return new_milli, billed_cents + chargeable, chargeable


def remaining_credit_minutes(balance_cents: int, price_cents: int, total_minutes: int) -> float:
    """Quantos minutos de sessão o saldo atual ainda paga."""
    per_min = minute_cost_millicents(price_cents, total_minutes)
    if per_min <= 0:
        return float("inf")
    return (balance_cents * 1000) / per_min


# ---------------- Manutenção ----------------
def maintenance_on(con: sqlite3.Connection) -> bool:
    return get_setting(con, "maintenance_mode", "0") == "1"


def maintenance_message(con: sqlite3.Connection) -> str:
    return get_setting(
        con, "maintenance_message", "Manutenção em andamento."
    ) or "Manutenção em andamento."


# ---------------- Tempo ----------------
def parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
