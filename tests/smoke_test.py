"""Smoke test de integração: cria o bot, carrega as 7 extensões, lista comandos
slash e confere a existência dos métodos de cola entre cogs. Não conecta ao Discord.

Rodar com: python3 tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

os.environ["OWNER_DISCORD_ID"] = "123"

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import core.config as cfg  # noqa: E402
from discord.ext import tasks  # noqa: E402

# Banco temporário — não toca em data/ real.
_tmp = tempfile.mkdtemp(prefix="hapination_smoke_")
cfg.DB_PATH = Path(_tmp) / "smoke.db"
cfg.BACKUP_DIR = Path(_tmp) / "backups"
cfg.BACKUP_DIR.mkdir(parents=True, exist_ok=True)

from bot import HapinationBot, INITIAL_EXTENSIONS  # noqa: E402


async def main() -> int:
    falhas = 0
    bot = HapinationBot()
    carregadas = []
    try:
        for ext in INITIAL_EXTENSIONS:
            try:
                await bot.load_extension(ext)
                carregadas.append(ext)
                print(f"  [OK] extensão carregada: {ext}")
            except Exception as exc:
                falhas += 1
                print(f"  [FALHA] extensão {ext}: {exc!r}")

        # 1) Lista de comandos slash registrados na árvore.
        comandos = list(bot.tree.walk_commands())
        nomes = [c.qualified_name for c in comandos]
        print(f"\nTotal de comandos na árvore: {len(comandos)}")
        for n in sorted(nomes):
            print(f"   /{n}")

        for obrigatorio in ("saldo", "precos", "horas"):
            if obrigatorio not in nomes:
                falhas += 1
                print(f"  [FALHA] comando /{obrigatorio} não encontrado na árvore.")
            else:
                print(f"  [OK] /{obrigatorio} presente.")

        # 2) Cogs registrados com os nomes corretos.
        nomes_cogs = set(bot.cogs.keys())
        print(f"\nCogs carregados: {sorted(nomes_cogs)}")
        for esperado in ("UsersCredits", "SessionsPrices", "VideosCog", "PanelCog", "AdminCog", "SupportBackground", "Publicos"):
            if esperado not in nomes_cogs:
                falhas += 1
                print(f"  [FALHA] cog {esperado} não registrado.")
            else:
                print(f"  [OK] cog {esperado} registrado.")

        # 3) Métodos de cola do UsersCredits (suspend_user / reactivate_user / set_nickname).
        uc = bot.get_cog("UsersCredits")
        if uc is None:
            falhas += 1
            print("  [FALHA] UsersCredits é None.")
        else:
            for metodo in ("suspend_user", "reactivate_user", "set_nickname", "notify_credit_change", "is_suspended"):
                fn = getattr(uc, metodo, None)
                if callable(fn) and inspect.iscoroutinefunction(fn):
                    print(f"  [OK] UsersCredits.{metodo} é async callable.")
                else:
                    falhas += 1
                    print(f"  [FALHA] UsersCredits.{metodo} não é async callable.")

        # 4) force_end_session no SessionsPrices.
        sp = bot.get_cog("SessionsPrices")
        if sp is None:
            falhas += 1
            print("  [FALHA] SessionsPrices é None.")
        else:
            fn = getattr(sp, "force_end_session", None)
            if callable(fn) and inspect.iscoroutinefunction(fn):
                print("  [OK] SessionsPrices.force_end_session é async callable.")
            else:
                falhas += 1
                print("  [FALHA] SessionsPrices.force_end_session não é async callable.")

        # 5) VideosCog acessível pelo nome real (valida a correção do fallback).
        v = bot.get_cog("VideosCog") or bot.get_cog("Videos")
        if v is None:
            falhas += 1
            print("  [FALHA] VideosCog não encontrado por nenhum dos nomes.")
        else:
            print(f"  [OK] vídeo cog encontrado (nome real: {v.__cog_name__}).")

    finally:
        # Cancela todas as tasks.loop iniciadas pelos cogs.
        for cog in bot.cogs.values():
            for attr_name in dir(cog):
                try:
                    attr = getattr(cog, attr_name)
                except Exception:
                    continue
                if isinstance(attr, tasks.Loop):
                    try:
                        attr.cancel()
                    except Exception:
                        pass
        # Dá um ciclo para as tasks cancelarem.
        await asyncio.sleep(0.1)
        pendentes = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in pendentes:
            t.cancel()
        if pendentes:
            await asyncio.gather(*pendentes, return_exceptions=True)
        await bot.close()

    print(f"\nExtensões carregadas com sucesso: {len(carregadas)}/{len(INITIAL_EXTENSIONS)}")
    if falhas:
        print(f"SMOKE TEST: {falhas} falha(s).")
        return 1
    print("SMOKE TEST: TODAS AS VERIFICAÇÕES PASSARAM.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
