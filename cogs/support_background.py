"""Cog de suporte e rotinas de fundo — Hapination Live.
Responsabilidades (especificação SPEC.md seção 2.6):

  1. Tickets de suporte: botão persistente "📩 Abrir atendimento" em 🎫・suporte
     (custom_id="hap_open_ticket", timeout=None, registrado via bot.add_view no
     cog_load). Ao clicar (usuário não suspenso, sem ticket aberto): cria canal
     privado `🎫・ticket-<id>` na categoria 🎬 HAPINATION LIVE (busca por nome
     normalizado; cria se não existir), overwrites restritos (usuário +
     proprietário + admins leem/escrevem, @everyone negado), registra em
     `tickets` (status='open', last_activity_at), posta embed com botão
     "🔒 Fechar atendimento". Fechamento manual (usuário ou equipe) com
     confirmação → marca closed, closed_at, closed_by, registra em 📋・registros
     (se o canal existir; senão log), apaga o canal após 10s. Listener
     on_message atualiza last_activity_at em canais de ticket aberto e SEMPRE
     chama bot.process_commands(message).
  2. Fechamento automático: loop a cada 1h → tickets abertos com inatividade >
     settings ticket_auto_close_hours (padrão 24h) são fechados, com notificação
     ao usuário e à equipe e registro.
  3. Limpeza de vídeos: loop a cada 6h → chama VideosCog.cleanup_old() (com
     fallback de nome e tratamento amigável se o cog estiver ausente).
  4. Backups automáticos: loop diário (tasks.loop hours=24) → em asyncio.to_thread
     roda core.database.backup_db para data/backups/backup_<ts>.db, registra em
     `backups` (filename, path, size_bytes, status), apaga backups mais antigos
     que settings backup_retention_days (padrão 30); em caso de falha notifica o
     proprietário por DM com detalhe.
  5. Comandos /admin backup (criar/listar: admin+owner; restaurar: SÓ owner com
     confirmação em 2 etapas; exportar: admin+owner envia o .db por DM ao owner).
     Restauração: valida o arquivo (abre conexão, checa schema_version), faz
     backup do estado atual, sobrescreve o banco principal via API sqlite backup
     (conexão do arquivo de origem faz .backup para destino = DB_PATH), roda
     core.database.migrate e avisa que o bot deve ser reiniciado.
  6. Limpeza de órfãos: loop a cada 6h remove arquivos em data/uploads cujo
     registro diz deleted=1 há >1h (cuidadoso, try/except, log).

Nome do cog registrado: "SupportBackground" (sem sufixo Cog), conforme
__cog_name__. Outros cogs são chamados por bot.get_cog com fallback de nome
("VideosCog"/"Videos", "SessionsPrices"/"SessionsPricesCog",
"UsersCredits"/"UsersCreditsCog") e None é tratado com erro amigável.

NÃO modifica core/, bot.py, SPEC.md nem outros cogs. UI e comentários em
português. Erros via core.common.log (nunca print). I/O demorado em
asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.common import (
    admin_only,
    check_cooldown,
    embed,
    error_embed,
    info_embed,
    is_admin,
    is_owner,
    is_suspended,
    log,
    notify_user,
    owner_only,
    success_embed,
    warn_embed,
)
from core.database import backup_db, get_db, get_int_setting, get_setting, migrate, set_setting

# ---------------------------------------------------------------------------
# Utilitários locais
# ---------------------------------------------------------------------------
def _normalize(name: str) -> str:
    """Normaliza nome de canal/categoria para comparação (sem acentos/emojis)."""
    s = unicodedata.normalize("NFKD", name or "")
    s = s.encode("ascii", "ignore").decode("ascii", errors="ignore")
    return re.sub(r"[^\w]+", "", s.lower())


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# Views persistentes (timeout=None, custom_id com prefixo hap_)
# ---------------------------------------------------------------------------
class OpenTicketView(discord.ui.View):
    """Botão persistente "📩 Abrir atendimento" (em 🎫・suporte)."""

    def __init__(self, cog: "SupportBackgroundCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="📩 Abrir atendimento",
        custom_id="hap_open_ticket",
        style=discord.ButtonStyle.primary,
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog.open_ticket(interaction)


class CloseTicketView(discord.ui.View):
    """Botão persistente "🔒 Fechar atendimento" (dentro do canal de ticket).
    Identifica o ticket pelo canal onde foi clicado, sobrevive a reinícios."""

    def __init__(self, cog: "SupportBackgroundCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🔒 Fechar atendimento",
        custom_id="hap_close_ticket",
        style=discord.ButtonStyle.danger,
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog.request_close_ticket(interaction)


class ConfirmCloseTicketView(discord.ui.View):
    """Confirmação de fechamento (transiente, ephemeral, 60s)."""

    def __init__(self, cog: "SupportBackgroundCog", ticket_id: int, actor_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.ticket_id = ticket_id
        self.actor_id = actor_id

    def _disable(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Não autorizado",
                    "Apenas quem solicitou o fechamento pode confirmar.",
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirmar fechamento", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._check_actor(interaction):
            return
        self._disable()
        try:
            await interaction.response.edit_message(
                embed=info_embed(
                    "Fechando atendimento",
                    "Este canal será excluído em 10 segundos.",
                ),
                view=self,
            )
        except Exception:
            log.exception("Falha ao editar mensagem de confirmação de fechamento")
        await self.cog.close_ticket_by_id(
            self.ticket_id,
            closed_by=interaction.user.id,
            reason="fechamento manual",
            notify_user=True,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._check_actor(interaction):
            return
        self._disable()
        try:
            await interaction.response.edit_message(
                embed=info_embed("Fechamento cancelado", "O atendimento continua aberto."),
                view=self,
            )
        except Exception:
            log.exception("Falha ao cancelar fechamento de ticket")
        self.stop()

    async def on_timeout(self) -> None:
        self.stop()


class ConfirmRestoreView(discord.ui.View):
    """Confirmação de restauração de backup (transiente, SÓ proprietário)."""

    def __init__(self, cog: "SupportBackgroundCog", backup_id: int, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.backup_id = backup_id
        self.owner_id = owner_id

    def _disable(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="✅ SIM, restaurar este backup", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Restauração é exclusiva do proprietário."),
                ephemeral=True,
            )
            return
        self._disable()
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.edit_original_response(
                embed=info_embed(
                    "Restaurando backup",
                    "Validando, fazendo backup de segurança e sobrescrevendo o banco. "
                    "Aguarde...",
                ),
                view=self,
            )
        except Exception:
            pass
        ok, msg = await self.cog.execute_restore(self.backup_id)
        emb = success_embed("Backup restaurado", msg) if ok else error_embed("Falha na restauração", msg)
        try:
            await interaction.edit_original_response(embed=emb, view=None)
        except Exception:
            log.exception("Falha ao responder resultado da restauração")
        self.stop()

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Apenas o proprietário pode decidir."),
                ephemeral=True,
            )
            return
        self._disable()
        try:
            await interaction.response.edit_message(
                embed=info_embed("Restauração cancelada", "O banco atual foi mantido."),
                view=self,
            )
        except Exception:
            log.exception("Falha ao cancelar restauração")
        self.stop()

    async def on_timeout(self) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class SupportBackgroundCog(commands.Cog, name="SupportBackground"):
    """Tickets de suporte + loops de fundo (limpeza, backups, órfãos).

    Registrado com nome "SupportBackground" (sem sufixo Cog): em discord.py
    2.x o nome efetivo vem do kwarg ``name=`` da classe (CogMeta ignora o
    atributo ``__cog_name__`` definido no corpo). O atributo abaixo é mantido
    por clareza e pela convenção de nomenclatura do projeto."""

    __cog_name__ = "SupportBackground"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Cache O(1) de channel_ids de tickets abertos (evita query a cada mensagem).
        self._open_ticket_channels: set[int] = set()
        self._ready_done = False
        self._backup_group: Optional[app_commands.Group] = None

    # -- ciclo de vida ------------------------------------------------------
    async def cog_load(self) -> None:
        # Views persistentes (sobrevivem a reinícios).
        self.bot.add_view(OpenTicketView(self))
        self.bot.add_view(CloseTicketView(self))
        # Carrega cache de tickets abertos.
        try:
            with get_db() as con:
                rows = con.execute(
                    "SELECT channel_id FROM tickets WHERE status='open'"
                ).fetchall()
            self._open_ticket_channels = {int(r["channel_id"]) for r in rows}
        except Exception:
            log.exception("SupportBackground: falha ao carregar cache de tickets abertos")
        # Registra o subgrupo /admin backup de forma idempotente (compartilhado
        # com UsersCreditsCog / SessionsPricesCog / AdminCog).
        try:
            await self._register_backup_group()
        except Exception:
            log.exception("SupportBackground: falha ao registrar /admin backup")
        # Inicia os loops de fundo.
        for loop in (
            self.ticket_auto_close_loop,
            self.video_cleanup_loop,
            self.orphan_cleanup_loop,
            self.backup_loop,
        ):
            if not loop.is_running():
                try:
                    loop.start()
                except RuntimeError:
                    pass  # já em execução
                except Exception:
                    log.exception("SupportBackground: falha ao iniciar loop %s", loop.coro.__name__)
        log.info("SupportBackground carregado: views persistentes e loops de fundo prontos.")

    async def cog_unload(self) -> None:
        for loop in (
            self.ticket_auto_close_loop,
            self.video_cleanup_loop,
            self.orphan_cleanup_loop,
            self.backup_loop,
        ):
            if loop.is_running():
                loop.cancel()

    async def _register_backup_group(self) -> None:
        tree = self.bot.tree
        admin_cmd = tree.get_command("admin")
        if admin_cmd is None:
            admin_cmd = app_commands.Group(
                name="admin", description="Comandos administrativos do Hapination Live"
            )
            tree.add_command(admin_cmd)
        if isinstance(admin_cmd, app_commands.Group) and admin_cmd.get_command("backup") is None:
            g = app_commands.Group(
                name="backup", description="Gerenciar backups do banco de dados"
            )
            g.command(name="criar", description="Cria um backup manual agora (equipe)")(
                self._backup_criar
            )
            g.command(name="listar", description="Lista os backups recentes (equipe)")(
                self._backup_listar
            )
            g.command(
                name="restaurar",
                description="Restaura um backup (SÓ proprietário, confirmação em 2 etapas)",
            )(self._backup_restaurar)
            g.command(
                name="exportar",
                description="Envia o arquivo .db de um backup por DM ao proprietário",
            )(self._backup_exportar)
            admin_cmd.add_command(g)
            self._backup_group = g
            log.info("Subgrupo /admin backup registrado por SupportBackgroundCog.")

    # -- listeners ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready_done:
            return
        self._ready_done = True
        # Garante a mensagem com o botão de abrir atendimento em 🎫・suporte.
        for guild in self.bot.guilds:
            try:
                await self._ensure_support_message(guild)
            except Exception:
                log.exception(
                    "SupportBackground: falha ao garantir mensagem de suporte na guilda %s",
                    guild.id,
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Atualiza last_activity_at de tickets abertos. SEMPRE processa comandos."""
        try:
            if message.guild is None:
                return
            if message.channel.id in self._open_ticket_channels:
                try:
                    with get_db() as con:
                        con.execute(
                            "UPDATE tickets SET last_activity_at=CURRENT_TIMESTAMP "
                            "WHERE channel_id=? AND status='open'",
                            (message.channel.id,),
                        )
                except Exception:
                    log.exception("Falha ao atualizar last_activity_at do ticket")
        finally:
            await self.bot.process_commands(message)

    # ------------------------------------------------------------------
    # Helpers de estrutura (categoria / canais / equipe)
    # ------------------------------------------------------------------
    async def _find_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        """Busca a categoria 🎬 HAPINATION LIVE por nome normalizado; cria se ausente."""
        target = _normalize(config.CATEGORY_NAME)
        for c in guild.categories:
            if _normalize(c.name) == target:
                return c
        category = await guild.create_category(
            config.CATEGORY_NAME, reason="Categoria do Hapination Live"
        )
        log.info("SupportBackground: categoria criada em %s: %s", guild.id, config.CATEGORY_NAME)
        return category

    def _staff_ids(self) -> list[int]:
        ids: set[int] = set()
        if config.OWNER_DISCORD_ID:
            ids.add(config.OWNER_DISCORD_ID)
        try:
            with get_db() as con:
                for row in con.execute("SELECT user_id FROM admins"):
                    ids.add(int(row["user_id"]))
        except Exception:
            log.exception("SupportBackground: erro ao listar admins")
        return sorted(ids)

    async def _ticket_overwrites(
        self, guild: discord.Guild, user: discord.Member | discord.User
    ) -> dict[Any, discord.PermissionOverwrite]:
        ow: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me is not None:
            ow[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                manage_channels=True, read_message_history=True, embed_links=True,
                attach_files=True,
            )
        ow[user] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, attach_files=True,
        )
        for uid in self._staff_ids():
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if member is not None and member.id != user.id:
                ow[member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True,
                    read_message_history=True,
                )
        return ow

    async def _get_registros_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Localiza 📋・registros (por settings, por nome normalizado)."""
        try:
            with get_db() as con:
                raw = get_setting(con, "channel_registros", None)
            if raw and raw.isdigit():
                ch = self.bot.get_channel(int(raw))
                if isinstance(ch, discord.TextChannel):
                    return ch
        except Exception:
            log.exception("Falha ao ler channel_registros")
        target = _normalize(config.CHANNELS.get("registros", "registros"))
        for c in guild.text_channels:
            if _normalize(c.name) == target:
                return c
        return None

    async def _log_registros(self, guild: discord.Guild, emb: discord.Embed) -> None:
        """Registra em 📋・registros; se o canal não existir, só loga."""
        channel = await self._get_registros_channel(guild)
        if channel is None:
            log.warning("Canal 📋・registros não encontrado em %s — registro: %s", guild.id, emb.description)
            return
        try:
            await channel.send(embed=emb)
        except Exception:
            log.exception("Falha ao registrar em 📋・registros (guilda %s)", guild.id)

    async def _notify_owner(self, emb: discord.Embed) -> bool:
        if not config.OWNER_DISCORD_ID:
            log.warning("OWNER_DISCORD_ID não configurado — não foi possível notificar o proprietário.")
            return False
        return await notify_user(self.bot, config.OWNER_DISCORD_ID, emb)

    def _get_videos_cog(self):
        for name in ("VideosCog", "Videos"):
            cog = self.bot.get_cog(name)
            if cog is not None:
                return cog
        return None

    # ------------------------------------------------------------------
    # Mensagem de suporte (botão em 🎫・suporte) — idempotente
    # ------------------------------------------------------------------
    async def _ensure_support_message(self, guild: discord.Guild) -> None:
        # Localiza o canal 🎫・suporte.
        channel: discord.TextChannel | None = None
        try:
            with get_db() as con:
                raw = get_setting(con, "channel_suporte", None)
            if raw and raw.isdigit():
                ch = self.bot.get_channel(int(raw))
                if isinstance(ch, discord.TextChannel):
                    channel = ch
        except Exception:
            pass
        if channel is None:
            target = _normalize(config.CHANNELS.get("suporte", "suporte"))
            for c in guild.text_channels:
                if _normalize(c.name) == target:
                    channel = c
                    break
        if channel is None:
            log.warning("Canal 🎫・suporte não encontrado em %s — mensagem de atendimento não postada.", guild.id)
            return
        # Já existe uma mensagem nossa?
        try:
            with get_db() as con:
                old_id = get_setting(con, "message_suporte_id", None)
            if old_id and old_id.isdigit():
                try:
                    await channel.fetch_message(int(old_id))
                    return  # já existe, não duplica
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
        except Exception:
            pass
        emb = embed(
            "📩 Atendimento / Suporte",
            "Precisa de ajuda com suas sessões simuladas, créditos fictícios ou vídeos?\n"
            "Clique no botão abaixo para abrir um atendimento privado com a equipe.",
        )
        try:
            msg = await channel.send(embed=emb, view=OpenTicketView(self))
            try:
                await msg.pin()
            except Exception:
                pass
            with get_db() as con:
                set_setting(con, "message_suporte_id", str(msg.id))
            log.info("Mensagem de atendimento postada em %s (id=%s)", guild.id, msg.id)
        except Exception:
            log.exception("Falha ao postar mensagem de atendimento em %s", guild.id)

    # ------------------------------------------------------------------
    # 1) Abertura de ticket
    # ------------------------------------------------------------------
    async def open_ticket(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "open_ticket"):
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Indisponível", "Abra um atendimento de dentro da guilda."),
                ephemeral=True,
            )
            return
        uid = interaction.user.id
        if is_suspended(uid):
            await interaction.response.send_message(
                embed=error_embed(
                    "Conta suspensa",
                    "Sua conta está suspensa. Procure o proprietário para mais informações.",
                ),
                ephemeral=True,
            )
            return
        # Já existe ticket aberto?
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
        except Exception:
            log.exception("Falha ao verificar ticket existente")
            row = None
        if row is not None:
            ch = self.bot.get_channel(int(row["channel_id"]))
            mention = ch.mention if isinstance(ch, discord.TextChannel) else f"<#{row['channel_id']}>"
            await interaction.response.send_message(
                embed=warn_embed(
                    "Você já tem um atendimento aberto",
                    f"Continue em {mention}. Feche-o antes de abrir outro.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            category = await self._find_category(interaction.guild)
            overwrites = await self._ticket_overwrites(interaction.guild, interaction.user)
            channel = await interaction.guild.create_text_channel(
                f"🎫・ticket-{uid}",
                category=category,
                overwrites=overwrites,
                reason=f"Ticket de suporte de {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Sem permissão",
                    "O bot não tem permissão para criar canais. Peça ao proprietário para verificar.",
                ),
                ephemeral=True,
            )
            return
        except Exception:
            log.exception("Falha ao criar canal de ticket")
            await interaction.followup.send(
                embed=error_embed("Erro", "Não foi possível abrir o atendimento. Tente novamente."),
                ephemeral=True,
            )
            return
        try:
            with get_db() as con:
                cur = con.execute(
                    "INSERT INTO tickets(user_id, channel_id, status, opened_at, last_activity_at) "
                    "VALUES (?, ?, 'open', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (uid, channel.id),
                )
                ticket_id = cur.lastrowid
            self._open_ticket_channels.add(channel.id)
        except Exception:
            log.exception("Falha ao registrar ticket no banco")
            try:
                await channel.delete(reason="falha ao registrar ticket")
            except Exception:
                pass
            await interaction.followup.send(
                embed=error_embed("Erro", "Não foi possível registrar o atendimento."),
                ephemeral=True,
            )
            return
        emb = embed(
            "🎫 Atendimento aberto",
            f"Olá {interaction.user.mention}! Descreva seu problema nesta conversa. "
            "A equipe (proprietário e administradores) foi notificada e responderá em breve.\n"
            f"**Ticket #{ticket_id}** — sessões simuladas • créditos fictícios.",
        )
        try:
            await channel.send(
                content=interaction.user.mention, embed=emb, view=CloseTicketView(self)
            )
        except Exception:
            log.exception("Falha ao postar embed de boas-vindas no ticket %s", ticket_id)
        # Registro em 📋・registros.
        reg = info_embed(
            "Ticket aberto",
            f"**#{ticket_id}** • usuário <@{uid}> • canal {channel.mention}",
        )
        await self._log_registros(interaction.guild, reg)
        await interaction.followup.send(
            embed=success_embed("Atendimento aberto", f"Seu canal privado: {channel.mention}"),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Fechamento de ticket (manual com confirmação + automático)
    # ------------------------------------------------------------------
    async def request_close_ticket(self, interaction: discord.Interaction) -> None:
        """Botão 🔒 Fechar atendimento: valida e pede confirmação."""
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                embed=error_embed("Indisponível", "Use este botão dentro de um canal de ticket."),
                ephemeral=True,
            )
            return
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM tickets WHERE channel_id=? AND status='open'",
                    (interaction.channel.id,),
                ).fetchone()
        except Exception:
            log.exception("Falha ao buscar ticket para fechamento")
            row = None
        if row is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Ticket não encontrado",
                    "Este canal não é um ticket aberto. Ele pode ser excluído manualmente.",
                ),
                ephemeral=True,
            )
            return
        actor = interaction.user.id
        if not (is_admin(actor) or int(row["user_id"]) == actor):
            await interaction.response.send_message(
                embed=error_embed(
                    "Acesso negado",
                    "Apenas o dono do ticket ou a equipe podem fechar este atendimento.",
                ),
                ephemeral=True,
            )
            return
        emb = warn_embed(
            "Confirmar fechamento?",
            f"Ticket **#{row['id']}** (usuário <@{row['user_id']}>).\n"
            "O canal será **excluído em 10 segundos** após a confirmação. "
            "Esta ação não pode ser desfeita.",
        )
        await interaction.response.send_message(
            embed=emb, view=ConfirmCloseTicketView(self, int(row["id"]), actor), ephemeral=True
        )

    async def close_ticket_by_id(
        self,
        ticket_id: int,
        *,
        closed_by: int,
        reason: str,
        notify_user: bool = True,
    ) -> None:
        """Marca closed, registra em 📋・registros, notifica o usuário e agenda
        a exclusão do canal após 10s. Idempotente (só afeta status='open')."""
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM tickets WHERE id=? AND status='open'", (ticket_id,)
                ).fetchone()
                if row is None:
                    return
                con.execute(
                    "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP, "
                    "closed_by=? WHERE id=?",
                    (closed_by, ticket_id),
                )
            channel_id = int(row["channel_id"])
            uid = int(row["user_id"])
            self._open_ticket_channels.discard(channel_id)
        except Exception:
            log.exception("Falha ao marcar ticket %s como fechado", ticket_id)
            return

        # Registro em 📋・registros (acha a guilda pelo canal).
        guild: discord.Guild | None = None
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            guild = channel.guild
        else:
            for g in self.bot.guilds:
                if g.get_channel(channel_id) is not None:
                    guild = g
                    break
        actor_mention = f"<@{closed_by}>" if closed_by else "sistema"
        reg = info_embed(
            "Ticket fechado",
            f"**#{ticket_id}** • usuário <@{uid}> • fechado por {actor_mention} • motivo: {reason}",
        )
        if guild is not None:
            await self._log_registros(guild, reg)
        else:
            log.info("Ticket #%s fechado (%s), mas guilda do canal não encontrada para registro.", ticket_id, reason)

        if notify_user:
            emb = info_embed(
                "Atendimento fechado",
                f"Seu ticket **#{ticket_id}** foi fechado.\nMotivo: {reason}.\n"
                "Se precisar novamente, abra outro atendimento em 🎫・suporte.",
            )
            await notify_user(self.bot, uid, emb)

        # Exclui o canal após 10s (em task separada para não bloquear).
        asyncio.create_task(self._delete_channel_after(channel_id, delay=10))

    async def _delete_channel_after(self, channel_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                for g in self.bot.guilds:
                    channel = g.get_channel(channel_id)
                    if channel is not None:
                        break
            if channel is not None and isinstance(channel, discord.TextChannel):
                await channel.delete(reason="Ticket de suporte fechado")
                log.info("Canal de ticket %s excluído.", channel_id)
        except Exception:
            log.exception("Falha ao excluir canal de ticket %s", channel_id)

    # ------------------------------------------------------------------
    # 2) Loop: fechamento automático de tickets inativos (a cada 1h)
    # ------------------------------------------------------------------
    @tasks.loop(hours=1)
    async def ticket_auto_close_loop(self) -> None:
        try:
            with get_db() as con:
                hours = get_int_setting(
                    con, "ticket_auto_close_hours", config.DEFAULT_TICKET_AUTO_CLOSE_HOURS
                )
                rows = con.execute(
                    "SELECT * FROM tickets WHERE status='open' "
                    "AND last_activity_at <= datetime('now', ?)",
                    (f"-{hours} hours",),
                ).fetchall()
            bot_id = self.bot.user.id if self.bot.user else 0
            for row in rows:
                try:
                    await self.close_ticket_by_id(
                        int(row["id"]),
                        closed_by=bot_id,
                        reason=f"inatividade > {hours}h (automático)",
                        notify_user=True,
                    )
                except Exception:
                    log.exception("Falha ao fechar ticket inativo %s", row["id"])
        except Exception:
            log.exception("Erro no loop de fechamento automático de tickets")

    # ------------------------------------------------------------------
    # 3) Loop: limpeza de vídeos (a cada 6h) → VideosCog.cleanup_old()
    # ------------------------------------------------------------------
    @tasks.loop(hours=6)
    async def video_cleanup_loop(self) -> None:
        cog = self._get_videos_cog()
        if cog is None or not hasattr(cog, "cleanup_old"):
            log.warning("VideosCog.cleanup_old indisponível — limpeza de vídeos pulada.")
            return
        try:
            n = await cog.cleanup_old()
            log.info("Limpeza de vídeos concluída: %s vídeo(s) removido(s).", n)
        except Exception:
            log.exception("Erro na limpeza automática de vídeos")

    # ------------------------------------------------------------------
    # 6) Loop: limpeza de órfãos em data/uploads (a cada 6h)
    # ------------------------------------------------------------------
    @tasks.loop(hours=6)
    async def orphan_cleanup_loop(self) -> None:
        try:
            await asyncio.to_thread(self._clean_orphans_sync)
        except Exception:
            log.exception("Erro na limpeza de arquivos órfãos")

    def _clean_orphans_sync(self) -> None:
        """Remove arquivos de vídeos marcados deleted=1 há >1h. Cuidadoso:
        só apaga arquivos dentro de UPLOAD_DIR ou THUMBNAIL_DIR."""
        upload_root = config.UPLOAD_DIR.resolve()
        thumb_root = config.THUMBNAIL_DIR.resolve()
        try:
            with get_db() as con:
                rows = con.execute(
                    "SELECT path, thumbnail_path FROM videos WHERE deleted=1 "
                    "AND deleted_at <= datetime('now', '-1 hour')"
                ).fetchall()
        except Exception:
            log.exception("Falha ao listar vídeos deletados para limpeza de órfãos")
            return
        removed = 0
        for r in rows:
            for key in ("path", "thumbnail_path"):
                p_raw = r[key]
                if not p_raw:
                    continue
                try:
                    p = Path(p_raw).resolve()
                    if not (p.is_relative_to(upload_root) or p.is_relative_to(thumb_root)):
                        log.warning("Pulando arquivo fora das áreas permitidas: %s", p)
                        continue
                    if p.exists() and p.is_file():
                        p.unlink(missing_ok=True)
                        removed += 1
                        log.info("Removido arquivo órfão: %s", p)
                except Exception as exc:
                    log.warning("Falha ao remover arquivo órfão %s: %s", p_raw, exc)
        # Remove subdiretórios vazios em data/uploads/<user_id> (não a raiz).
        try:
            if config.UPLOAD_DIR.exists():
                for sub in sorted(config.UPLOAD_DIR.iterdir(), reverse=True):
                    if sub.is_dir():
                        try:
                            # rmtree se vazio
                            any_file = any(sub.rglob("*"))
                            if not any_file:
                                sub.rmdir()
                        except Exception:
                            pass
        except Exception:
            pass
        if removed:
            log.info("Limpeza de órfãos concluída: %s arquivo(s) removido(s).", removed)

    # ------------------------------------------------------------------
    # 4) Backups (loop diário + comandos)
    # ------------------------------------------------------------------
    @tasks.loop(hours=24)
    async def backup_loop(self) -> None:
        try:
            # Evita backup imediato a cada reinício: se houve backup 'ok' nos
            # últimos 60min, pula esta iteração.
            with get_db() as con:
                recent = con.execute(
                    "SELECT 1 FROM backups WHERE status='ok' "
                    "AND created_at > datetime('now', '-60 minutes') LIMIT 1"
                ).fetchone()
            if recent:
                log.info("Backup automático pulado: já existe backup recente (<60min).")
                return
            info = await self._do_backup(manual=False)
            log.info(
                "Backup automático criado: #%s %s (%s).",
                info["id"], info["filename"], _fmt_bytes(info["size"]),
            )
            await self._purge_old_backups()
        except Exception as exc:
            log.exception("Falha no backup automático")
            await self._notify_owner(
                error_embed(
                    "Falha no backup automático",
                    f"O backup diário falhou: `{exc}`.\n"
                    "Verifique o espaço em disco e as permissões de data/backups/.",
                )
            )

    async def _do_backup(self, *, manual: bool) -> dict:
        """Executa backup em thread, registra em `backups`. Retorna dict com
        id/filename/size/path. Em falha registra status='failed' e relança."""
        fname = f"backup_{_ts()}.db"
        dest = config.BACKUP_DIR / fname
        try:
            await asyncio.to_thread(backup_db, dest)
            size = dest.stat().st_size
            with get_db() as con:
                cur = con.execute(
                    "INSERT INTO backups(filename, path, size_bytes, status) "
                    "VALUES (?, ?, ?, 'ok')",
                    (fname, str(dest), size),
                )
                bid = cur.lastrowid
            return {"id": bid, "filename": fname, "size": size, "path": str(dest)}
        except Exception as exc:
            try:
                with get_db() as con:
                    con.execute(
                        "INSERT INTO backups(filename, path, size_bytes, status) "
                        "VALUES (?, ?, 0, 'failed')",
                        (fname, str(dest)),
                    )
            except Exception:
                log.exception("Falha ao registrar backup com status='failed'")
            raise exc

    async def _purge_old_backups(self) -> None:
        """Apaga backups (arquivo + registro) mais antigos que
        backup_retention_days. Também varre arquivos .db órfãos no disco."""
        try:
            with get_db() as con:
                days = get_int_setting(
                    con, "backup_retention_days", config.DEFAULT_BACKUP_RETENTION_DAYS
                )
                rows = con.execute(
                    "SELECT * FROM backups WHERE created_at <= datetime('now', ?)",
                    (f"-{days} days",),
                ).fetchall()
                known_paths: set[str] = {
                    str(Path(r["path"]).resolve())
                    for r in con.execute("SELECT path FROM backups").fetchall()
                    if r["path"]
                }
            removed = 0
            for r in rows:
                p_raw = r["path"]
                try:
                    if p_raw:
                        Path(p_raw).unlink(missing_ok=True)
                    with get_db() as con:
                        con.execute("DELETE FROM backups WHERE id=?", (int(r["id"]),))
                    removed += 1
                except Exception as exc:
                    log.warning("Falha ao purgar backup #%s: %s", r["id"], exc)
            # Arquivos .db em disco sem registro (ex: pre_migration, pre_restore).
            backup_root = config.BACKUP_DIR.resolve()
            cutoff = time.time() - days * 86400
            for f in backup_root.glob("*.db"):
                try:
                    if str(f.resolve()) in known_paths:
                        continue
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                        log.info("Removido backup órfão antigo: %s", f)
                except Exception as exc:
                    log.warning("Falha ao remover backup órfão %s: %s", f, exc)
            if removed:
                log.info("Retenção de backups: %s registro(s) mais antigo(s) que %sd removido(s).", removed, days)
        except Exception:
            log.exception("Erro na purgação de backups antigos")

    # ------------------------------------------------------------------
    # 5) Comandos /admin backup
    # ------------------------------------------------------------------
    @admin_only
    async def _backup_criar(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "backup_criar"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            info = await self._do_backup(manual=True)
            await self._purge_old_backups()
            await interaction.followup.send(
                embed=success_embed(
                    "Backup criado",
                    f"**#{info['id']} — {info['filename']}**\n"
                    f"Tamanho: {_fmt_bytes(info['size'])}\n"
                    f"Local: `data/backups/`",
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Falha no backup manual")
            await interaction.followup.send(
                embed=error_embed("Falha no backup", f"Não foi possível criar o backup: `{exc}`"),
                ephemeral=True,
            )

    @admin_only
    async def _backup_listar(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "backup_listar"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            with get_db() as con:
                rows = con.execute(
                    "SELECT * FROM backups ORDER BY id DESC LIMIT 15"
                ).fetchall()
                total = con.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"]
            if not rows:
                await interaction.followup.send(
                    embed=info_embed("Sem backups", "Nenhum backup foi criado ainda."),
                    ephemeral=True,
                )
                return
            emb = embed(
                "Backups recentes",
                f"**{total}** registro(s) no total — mostrando os 15 mais recentes.",
            )
            for r in rows:
                status = r["status"] or "?"
                icon = "✅" if status == "ok" else ("⛔" if status == "failed" else "ℹ️")
                emb.add_field(
                    name=f"#{r['id']} — {r['filename']}",
                    value=(
                        f"{icon} status: **{status}** • {_fmt_bytes(int(r['size_bytes'] or 0))}\n"
                        f"Criado em: {r['created_at']} UTC"
                    ),
                    inline=False,
                )
            await interaction.followup.send(embed=emb, ephemeral=True)
        except Exception:
            log.exception("Falha ao listar backups")
            await interaction.followup.send(
                embed=error_embed("Erro", "Não foi possível listar os backups."),
                ephemeral=True,
            )

    @owner_only
    @app_commands.describe(backup_id="ID do backup (veja /admin backup listar)")
    async def _backup_restaurar(self, interaction: discord.Interaction, backup_id: int) -> None:
        """Etapa 1: valida o arquivo e pede confirmação (2 etapas). SÓ owner."""
        if not await check_cooldown(interaction, "backup_restaurar"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM backups WHERE id=?", (backup_id,)
                ).fetchone()
        except Exception:
            log.exception("Falha ao buscar backup %s", backup_id)
            row = None
        if row is None:
            await interaction.followup.send(
                embed=error_embed(
                    "Backup não encontrado",
                    f"Não existe backup com id **#{backup_id}**. Confira em `/admin backup listar`.",
                ),
                ephemeral=True,
            )
            return
        path = Path(row["path"] or "")
        if not path.exists():
            await interaction.followup.send(
                embed=error_embed(
                    "Arquivo ausente",
                    f"O arquivo do backup **#{backup_id}** não está mais em disco: `{row['path']}`.",
                ),
                ephemeral=True,
            )
            return
        # Valida: abre conexão e checa schema_version.
        try:
            valid = await asyncio.to_thread(self._validate_backup_sync, path)
        except Exception as exc:
            log.exception("Falha ao validar backup %s", backup_id)
            await interaction.followup.send(
                embed=error_embed("Falha na validação", f"Não foi possível abrir o backup: `{exc}`"),
                ephemeral=True,
            )
            return
        if not valid:
            await interaction.followup.send(
                embed=error_embed(
                    "Backup inválido",
                    f"O arquivo **#{backup_id}** não contém a tabela `schema_version` — "
                    "parece não ser um backup válido do Hapination Live.",
                ),
                ephemeral=True,
            )
            return
        emb = warn_embed(
            "Confirmar restauração?",
            f"Backup **#{row['id']} — {row['filename']}** ({_fmt_bytes(int(row['size_bytes'] or 0))}).\n\n"
            "**Esta ação vai:**\n"
            "1. Fazer um backup de segurança do estado atual;\n"
            "2. **Sobrescrever** o banco principal com este backup (API sqlite backup);\n"
            "3. Rodar migrações para igualar o esquema.\n\n"
            "Dados atuais (usuários, créditos, sessões, vídeos) serão **perdidos** e "
            "substituídos pelos do backup. O bot deve ser **reiniciado** em seguida.\n"
            "Confirme apenas se tiver certeza.",
        )
        await interaction.followup.send(
            embed=emb,
            view=ConfirmRestoreView(self, backup_id, interaction.user.id),
            ephemeral=True,
        )

    @staticmethod
    def _validate_backup_sync(path: Path) -> bool:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            return row is not None
        finally:
            con.close()

    async def execute_restore(self, backup_id: int) -> tuple[bool, str]:
        """Etapa 2: executa a restauração. Retorna (ok, mensagem)."""
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM backups WHERE id=?", (backup_id,)
                ).fetchone()
            if row is None:
                return False, f"Backup #{backup_id} não encontrado."
            src_path = Path(row["path"] or "")
            if not src_path.exists():
                return False, f"Arquivo do backup #{backup_id} ausente em disco."
        except Exception as exc:
            log.exception("Falha ao carregar backup para restauração")
            return False, f"Erro ao carregar backup: `{exc}`"

        def _restore_sync() -> str:
            # 1) Backup de segurança do estado atual (antes de sobrescrever).
            pre = config.BACKUP_DIR / f"pre_restore_{_ts()}.db"
            backup_db(pre)
            # 2) Sobrescreve o banco principal via API sqlite backup:
            #    conexão do arquivo de origem faz .backup para destino = DB_PATH.
            src = sqlite3.connect(str(src_path), timeout=30)
            dst = sqlite3.connect(str(config.DB_PATH), timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            # Remove WAL residual do destino para evitar inconsistência pós-restore.
            for suffix in ("-wal", "-shm"):
                try:
                    extra = Path(str(config.DB_PATH) + suffix)
                    if extra.exists():
                        extra.unlink()
                except Exception:
                    pass
            # 3) Migrações idempotentes sobre o banco restaurado.
            migrate()
            # Registra o backup pré-restauração.
            try:
                size = pre.stat().st_size if pre.exists() else 0
                with get_db() as con:
                    con.execute(
                        "INSERT INTO backups(filename, path, size_bytes, status) "
                        "VALUES (?, ?, ?, 'ok')",
                        (pre.name, str(pre), size),
                    )
            except Exception:
                log.exception("Falha ao registrar backup pré-restauração")
            return pre.name

        try:
            pre_name = await asyncio.to_thread(_restore_sync)
        except Exception as exc:
            log.exception("Falha ao restaurar backup %s", backup_id)
            return False, f"Erro durante a restauração: `{exc}`. Nenhuma alteração foi confirmada."

        log.warning("Banco restaurado a partir do backup #%s (pré-restauração: %s).", backup_id, pre_name)
        return (
            True,
            f"Backup **#{backup_id}** restaurado com sucesso.\n"
            f"Backup de segurança do estado anterior: `{pre_name}`.\n"
            "⚠️ **Reinicie o bot** para que todos os cogs assumam os dados restaurados.",
        )

    @admin_only
    @app_commands.describe(backup_id="ID do backup (veja /admin backup listar)")
    async def _backup_exportar(self, interaction: discord.Interaction, backup_id: int) -> None:
        if not await check_cooldown(interaction, "backup_exportar"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            with get_db() as con:
                row = con.execute(
                    "SELECT * FROM backups WHERE id=?", (backup_id,)
                ).fetchone()
        except Exception:
            log.exception("Falha ao buscar backup %s para exportar", backup_id)
            row = None
        if row is None or not Path(row["path"] or "").exists():
            await interaction.followup.send(
                embed=error_embed(
                    "Backup não encontrado",
                    f"Não existe backup com id **#{backup_id}** (ou o arquivo foi apagado).",
                ),
                ephemeral=True,
            )
            return
        if not config.OWNER_DISCORD_ID:
            await interaction.followup.send(
                embed=error_embed(
                    "Proprietário não configurado",
                    "Defina OWNER_DISCORD_ID no ambiente para poder exportar por DM.",
                ),
                ephemeral=True,
            )
            return
        try:
            user = self.bot.get_user(config.OWNER_DISCORD_ID) or await self.bot.fetch_user(
                config.OWNER_DISCORD_ID
            )
            if user is None:
                raise ValueError("usuário proprietário não encontrado")
            f = discord.File(row["path"], filename=row["filename"])
            await user.send(
                embed=info_embed(
                    f"Backup #{row['id']}",
                    f"Arquivo: **{row['filename']}** ({_fmt_bytes(int(row['size_bytes'] or 0))}).",
                ),
                file=f,
            )
            await interaction.followup.send(
                embed=success_embed(
                    "Backup exportado",
                    f"O arquivo **#{row['id']} — {row['filename']}** foi enviado por DM ao proprietário.",
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Falha ao exportar backup %s por DM", backup_id)
            await interaction.followup.send(
                embed=error_embed(
                    "Falha ao exportar",
                    f"Não foi possível enviar o backup por DM ao proprietário: `{exc}`.",
                ),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SupportBackgroundCog(bot))
