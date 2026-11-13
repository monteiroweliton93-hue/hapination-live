"""Hapination Live — ponto de entrada (fino).

Toda a lógica está em `core/` e `cogs/`. Este arquivo apenas configura o bot,
aplica migrações seguras do banco, carrega os cogs e inicia com lock de instância.
"""
from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from core import config
from core.common import handle_app_error, instance_lock, log
from core.database import migrate

INITIAL_EXTENSIONS = [
    "cogs.users_credits",
    "cogs.sessions_prices",
    "cogs.videos",
    "cogs.panel",
    "cogs.admin",
    "cogs.support_background",
    "cogs.publicos",
]


class HapinationBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # necessário para detectar uploads e tickets
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        # 1) Migrações idempotentes com backup prévio (preserva dados da FadeHost).
        await asyncio.to_thread(migrate)
        log.info("Banco de dados migrado e verificado.")

        # 2) Carregar cogs.
        carregadas: list[str] = []
        try:
            for ext in INITIAL_EXTENSIONS:
                await self.load_extension(ext)
                carregadas.append(ext)
                log.info("Cog carregado: %s", ext)
        except Exception:
            log.exception("Falha ao carregar cog essencial %s; inicialização cancelada.", ext)
            for loaded in reversed(carregadas):
                try:
                    await self.unload_extension(loaded)
                except Exception:
                    log.exception("Falha ao descarregar cog %s durante rollback.", loaded)
            raise

        # 3) Sincronizar comandos de aplicação.
        try:
            synced = await self.tree.sync()
            log.info("%d comandos sincronizados.", len(synced))
        except Exception:
            log.exception("Falha ao sincronizar comandos (tentará novamente no on_ready).")

    async def on_ready(self) -> None:
        log.info("Hapination Live conectado como %s (id=%s)", self.user, self.user.id)


async def main() -> None:
    if not config.TOKEN:
        raise SystemExit(
            "Defina DISCORD_BOT_TOKEN ou DISCORD_TOKEN no .env / variáveis de ambiente."
        )
    if not config.OWNER_DISCORD_ID:
        raise SystemExit("Defina OWNER_DISCORD_ID no .env / variáveis de ambiente.")

    async with HapinationBot() as bot:
        bot.tree.on_error = handle_app_error
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    with instance_lock():
        asyncio.run(main())
