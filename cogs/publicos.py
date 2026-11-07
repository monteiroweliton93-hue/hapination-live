"""Cog de comandos públicos preservados da versão antiga — Hapination Live.
Comandos de nível superior (qualquer usuário, sem grupo /admin):
  - /saldo   (ephemeral) — mostra o saldo de créditos fictícios do usuário.
  - /precos  — tabela de durações e preços das sessões simuladas.
  - /horas   — tabela de durações disponíveis (mesma base de /precos).
Créditos são fictícios. Dados privados SEMPRE em ephemeral.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.common import check_cooldown, info_embed, log, money
from core.database import ensure_user, get_db
from core.services import get_balance, list_durations


def _tabela_precos_embed(titulo: str) -> discord.Embed:
    """Monta embed preto+roxo com a tabela de durações/preços ativos."""
    with get_db() as con:
        duracoes = list_durations(con, only_active=True)
    if not duracoes:
        return info_embed(
            titulo,
            "Nenhuma duração está disponível no momento. "
            "Procure a administração do servidor.",
        )
    linhas = []
    for d in duracoes:
        horas = d["hours"]
        preco = money(d["price_cents"])
        if horas == 1:
            label = "1 hora"
        else:
            label = f"{horas} horas"
        linhas.append(f"• **{label}** — {preco} por sessão simulada")
    desc = (
        "Escolha a duração ao iniciar uma sessão. O valor é cobrado "
        "proporcionalmente minuto a minuto, com teto no preço total.\n\n"
        + "\n".join(linhas)
        + "\n\n_Créditos fictícios • sessões simuladas._"
    )
    emb = discord.Embed(
        title=titulo,
        description=desc,
        color=config.COLOR_PRIMARY,
    )
    emb.set_footer(text=config.FOOTER_TEXT)
    emb.timestamp = discord.utils.utcnow()
    return emb


class PublicosCog(commands.Cog, name="Publicos"):
    """Comandos públicos de consulta (saldo, preços, durações)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        log.info("PublicosCog carregado (/saldo, /precos, /horas).")

    @app_commands.command(
        name="saldo",
        description="Consulte seu saldo de créditos (visível só para você).",
    )
    async def cmd_saldo(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "saldo"):
            return
        with get_db() as con:
            ensure_user(con, interaction.user.id)
            saldo = get_balance(con, interaction.user.id)
        await interaction.response.send_message(
            embed=info_embed(
                "💳 Seu saldo",
                f"Saldo atual: **{money(saldo)}**\n\n"
                "_Créditos fictícios — Hapination Live (sessões simuladas)._",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="precos",
        description="Tabela de preços das sessões simuladas por duração.",
    )
    async def cmd_precos(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "precos"):
            return
        await interaction.response.send_message(
            embed=_tabela_precos_embed("💲 Preços das sessões simuladas"),
            ephemeral=True,
        )

    @app_commands.command(
        name="horas",
        description="Durações disponíveis para sessões simuladas (1h a 48h).",
    )
    async def cmd_horas(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "horas"):
            return
        await interaction.response.send_message(
            embed=_tabela_precos_embed("⏱️ Durações disponíveis"),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PublicosCog(bot))
