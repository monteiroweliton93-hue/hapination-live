"""Utilitários compartilhados: embeds (preto+roxo), checagens, cooldown,
logger, lock de instância única e notificações privadas.

UI em português. NUNCA exponha saldos ou nomes de arquivos privados em canais públicos.
"""
from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from typing import Any

import discord

from . import config
from .database import get_db

# ---------------- Logger ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_DIR / "hapination.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("hapination")


# ---------------- Formatação ----------------
def money(cents: int) -> str:
    """Formata centavos inteiros em R$ X,XX (créditos fictícios)."""
    return f"R$ {cents / 100:.2f}".replace(".", ",")


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}min"
    if m:
        return f"{m}min {sec}s"
    return f"{sec}s"


# ---------------- Embeds ----------------
def embed(
    title: str = "",
    description: str = "",
    color: int = config.COLOR_PRIMARY,
    footer: str | None = config.FOOTER_TEXT,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    if footer:
        e.set_footer(text=footer)
    e.timestamp = discord.utils.utcnow()
    return e


def success_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"✅ {title}", description, config.COLOR_SUCCESS)


def warn_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"⚠️ {title}", description, config.COLOR_WARN)


def error_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"⛔ {title}", description, config.COLOR_DANGER)


def info_embed(title: str, description: str = "") -> discord.Embed:
    return embed(f"ℹ️ {title}", description, config.COLOR_INFO)


# ---------------- Cooldown anti-repetição (10s) ----------------
_cooldowns: dict[tuple[int, str], float] = {}


def cooldown_remaining(user_id: int, action: str) -> float:
    """Retorna segundos restantes no cooldown (0 = liberado)."""
    key = (user_id, action)
    last = _cooldowns.get(key, 0.0)
    remaining = config.COMMAND_COOLDOWN_SECONDS - (time.time() - last)
    return max(0.0, remaining)


def cooldown_mark(user_id: int, action: str) -> None:
    _cooldowns[(user_id, action)] = time.time()


async def check_cooldown(interaction: discord.Interaction, action: str) -> bool:
    """Retorna True se passou no cooldown. Responde ephemeral e retorna False se bloqueado."""
    rem = cooldown_remaining(interaction.user.id, action)
    if rem > 0:
        await interaction.response.send_message(
            embed=warn_embed(
                "Aguarde um momento",
                f"Você poderá usar esta ação novamente em **{rem:.0f}s**.",
            ),
            ephemeral=True,
        )
        return False
    cooldown_mark(interaction.user.id, action)
    return True


# ---------------- Permissões ----------------
def is_owner(user_id: int) -> bool:
    return config.OWNER_DISCORD_ID != 0 and user_id == config.OWNER_DISCORD_ID


def is_admin(user_id: int) -> bool:
    """Proprietário é sempre admin. Demais admins ficam na tabela `admins`."""
    if is_owner(user_id):
        return True
    with get_db() as con:
        row = con.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
        return row is not None


def is_suspended(user_id: int) -> bool:
    with get_db() as con:
        row = con.execute(
            "SELECT status FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["status"] == "suspended")


# Decoradores para comandos de aplicação.
async def _owner_predicate(interaction: discord.Interaction) -> bool:
    if not is_owner(interaction.user.id):
        raise discord.app_commands.CheckFailure("owner_only")
    return True


async def _admin_predicate(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction.user.id):
        raise discord.app_commands.CheckFailure("admin_only")
    return True


async def _not_suspended_predicate(interaction: discord.Interaction) -> bool:
    if is_suspended(interaction.user.id):
        raise discord.app_commands.CheckFailure("suspended")
    return True


owner_only = discord.app_commands.check(_owner_predicate)
admin_only = discord.app_commands.check(_admin_predicate)
not_suspended = discord.app_commands.check(_not_suspended_predicate)


# ---------------- Lock de instância única ----------------
@contextmanager
def instance_lock():
    """Evita duas instâncias do bot rodando ao mesmo tempo. Faz exit se já houver uma."""
    fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            "Outra instância do Hapination Live já está rodando (lock em "
            f"{config.LOCK_PATH}). Encerre a outra antes de iniciar."
        )
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    try:
        yield fh
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


# ---------------- Notificações privadas ----------------
async def notify_user(bot: discord.Client, user_id: int, emb: discord.Embed) -> bool:
    """Tenta enviar DM privada. Retorna False se não conseguir (sem lançar)."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user is None:
            return False
        await user.send(embed=emb)
        return True
    except Exception as exc:
        log.warning("Não foi possível notificar o usuário %s: %s", user_id, exc)
        return False


# ---------------- Tratamento de erro global para comandos ----------------
async def handle_app_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
) -> None:
    if isinstance(error, discord.app_commands.CheckFailure):
        msg = str(error)
        if msg == "owner_only":
            e = error_embed("Acesso negado", "Esta ação é exclusiva do proprietário.")
        elif msg == "admin_only":
            e = error_embed("Acesso negado", "Esta ação é restrita à equipe administrativa.")
        elif msg == "suspended":
            e = error_embed(
                "Conta suspensa",
                "Sua conta está suspensa. Procure o proprietário para mais informações.",
            )
        else:
            e = error_embed("Acesso negado", "Você não tem permissão para esta ação.")
    elif isinstance(error, discord.app_commands.MissingPermissions):
        e = error_embed("Permissões insuficientes", "O bot não tem permissões necessárias.")
    else:
        log.exception("Erro em comando: %r", error)
        e = error_embed(
            "Erro inesperado",
            "Não foi possível concluir a operação. A equipe foi notificada.",
        )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
    except Exception:
        log.exception("Falha ao responder erro ao usuário")
