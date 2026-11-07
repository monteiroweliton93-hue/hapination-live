"""Painel público e painéis privados individuais do Hapination Live.

Responsabilidades (SPEC §2.4):
  1. Garantir, no on_ready, a categoria ``🎬 HAPINATION LIVE`` e os 5 canais
     (🎛️・painel, 🎫・suporte, 📢・avisos, 📋・registros, 🔒・administracao),
     preservando os já existentes (busca por nome normalizado) e guardando os
     IDs na tabela ``settings`` (chaves ``channel_<nome>``).
  2. Manter uma mensagem fixa (pin) em 🎛️painel com embed preto+roxo (imagem
     ``painel_tiktok.jpg`` se existir) e botão persistente "🎟️ Abrir meu painel"
     (``custom_id="hap_open_panel"``, ``timeout=None``, registrado em
     ``cog_load`` via ``bot.add_view``). O comando ``/painel`` (admin/owner)
     reenvia essa mensagem.
  3. Ao clicar em "Abrir meu painel", criar (ou reutilizar) o canal privado
     ``👤・painel-<id-do-usuario>`` na categoria, com overwrites restritos, e
     postar o embed do painel privado (saldo, sessões ativas, vídeos, histórico,
     tabela de preços) com 8 botões persistentes (prefixo ``hap_``).
  4. Os botões executam a ação diretamente (nunca respondem "use o comando /X"):
     delegam para ``VideosCog`` / ``SessionsPrices`` (ambos obtidos via
     ``bot.get_cog``, com fallback de nome e tratamento amigável se ausentes)
     ou respondem ephemeral com dados frescos.
  5. Suspensão e manutenção são verificadas antes das ações; cooldown de 10s
     na abertura do painel; exceções tratadas com ``core.common.log``.

NÃO modifique ``core/``, ``bot.py``, ``SPEC.md`` nem outros cogs.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.common import (
    admin_only,
    check_cooldown,
    embed,
    error_embed,
    info_embed,
    is_suspended,
    log,
    money,
    success_embed,
    warn_embed,
)
from core.database import get_db, get_setting, set_setting
from core import services


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def _normalize(name: str) -> str:
    """Normaliza nome de canal/categoria para comparação: remove acentos,
    emojis, pontuação (incluindo ・) e espaços, em minúsculas.

    Ex.: "🎬 HAPINATION LIVE" -> "hapinationlive"
         "🔒・administracao"  -> "administracao"
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = s.encode("ascii", "ignore").decode("ascii", errors="ignore")
    return re.sub(r"[^\w]+", "", s.lower())


def _pick(d: dict, *keys: str, default: Any = "?") -> Any:
    """Acessa, de forma defensiva, a primeira chave presente e não vazia
    em um dicionário retornado por outro cog (cuja estrutura exata não
    controlamos)."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return default


# ---------------------------------------------------------------------------
# Views persistentes (timeout=None, custom_id com prefixo hap_)
# ---------------------------------------------------------------------------
class PublicPanelView(discord.ui.View):
    """Botão persistente do painel público. Registrado via add_view no cog_load."""

    def __init__(self, cog: "PanelCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="🎟️ Abrir meu painel",
        custom_id="hap_open_panel",
        style=discord.ButtonStyle.primary,
    )
    async def open_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.open_user_panel(interaction)


class PrivatePanelView(discord.ui.View):
    """8 botões persistentes do painel privado individual.

    Todos executam a ação diretamente, delegando para os cogs especializados
    ou respondendo ephemeral. custom_id com prefixo ``hap_``.
    """

    def __init__(self, cog: "PanelCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📤 Enviar vídeo", custom_id="hap_panel_upload", style=discord.ButtonStyle.primary, row=0)
    async def btn_upload(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_enable_upload(interaction)

    @discord.ui.button(label="▶️ Iniciar sessão", custom_id="hap_panel_start", style=discord.ButtonStyle.success, row=0)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_start_session(interaction)

    @discord.ui.button(label="🔄 Renovar", custom_id="hap_panel_renew", style=discord.ButtonStyle.primary, row=0)
    async def btn_renew(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_renew_session(interaction)

    @discord.ui.button(label="⏹️ Encerrar", custom_id="hap_panel_end", style=discord.ButtonStyle.danger, row=0)
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_end_session(interaction)

    @discord.ui.button(label="🎬 Meus vídeos", custom_id="hap_panel_videos", style=discord.ButtonStyle.secondary, row=1)
    async def btn_videos(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_list_videos(interaction)

    @discord.ui.button(label="📜 Histórico", custom_id="hap_panel_history", style=discord.ButtonStyle.secondary, row=1)
    async def btn_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_history(interaction)

    @discord.ui.button(label="💲 Preços", custom_id="hap_panel_prices", style=discord.ButtonStyle.secondary, row=1)
    async def btn_prices(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_prices(interaction)

    @discord.ui.button(label="❌ Fechar painel", custom_id="hap_panel_close", style=discord.ButtonStyle.danger, row=1)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.action_close_panel(interaction)


class ConfirmCloseView(discord.ui.View):
    """Confirmação de fechamento (NÃO persistente — curta duração, ephemeral)."""

    def __init__(self, cog: "PanelCog") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="✅ Confirmar fechamento", style=discord.ButtonStyle.danger, custom_id="hap_close_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.confirm_close_panel(interaction)

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary, custom_id="hap_close_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await interaction.response.edit_message(
                embed=info_embed("Fechamento cancelado", "Seu painel continua aberto."),
                view=None,
            )
        except Exception:
            log.exception("Falha ao cancelar fechamento do painel")

    async def on_timeout(self) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class PanelCog(commands.Cog):
    """Gerencia a estrutura de canais e os painéis (público e privados)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ready_done = False  # evita trabalho repetido em on_ready múltiplos

    # -- registro das views persistentes ------------------------------------
    async def cog_load(self) -> None:
        # Sobrevive a reinícios: custom_ids registrados globalmente.
        self.bot.add_view(PublicPanelView(self))
        self.bot.add_view(PrivatePanelView(self))
        log.info("PanelCog: views persistentes registradas.")

    # -- listeners ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready_done:
            return
        self._ready_done = True
        for guild in self.bot.guilds:
            try:
                await self.ensure_structure(guild)
                await self.ensure_public_panel(guild)
            except Exception:
                log.exception("PanelCog: falha ao preparar estrutura/painel na guilda %s", guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Se o bot entrar em nova guilda, cria a estrutura lá também."""
        try:
            await self.ensure_structure(guild)
            await self.ensure_public_panel(guild)
        except Exception:
            log.exception("PanelCog: falha ao preparar estrutura na nova guilda %s", guild.id)

    # -- comando /painel (admin/owner) --------------------------------------
    @app_commands.command(name="painel", description="Reenvia a mensagem fixa do painel público (equipe)")
    @admin_only
    async def painel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(embed=error_embed("Indisponível", "Use este comando dentro de uma guilda."), ephemeral=True)
            return
        try:
            await self.ensure_structure(guild)
            channel = self._get_channel(guild, "painel")
            if channel is None:
                await interaction.followup.send(embed=error_embed("Canal ausente", "Não foi possível encontrar o canal 🎛️・painel."), ephemeral=True)
                return
            # Apaga a mensagem antiga do painel, se existir, para não duplicar.
            old_id = self._read_setting("message_painel_id")
            if old_id:
                try:
                    old_msg = await channel.fetch_message(int(old_id))
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException, ValueError):
                    pass
            msg = await self._post_public_panel(channel)
            await interaction.followup.send(
                embed=success_embed("Painel reenviado", f"Nova mensagem fixa publicada em {channel.mention} (id={msg.id})."),
                ephemeral=True,
            )
        except Exception:
            log.exception("PanelCog: erro ao reenviar painel público")
            await interaction.followup.send(embed=error_embed("Erro", "Não foi possível reenviar o painel público."), ephemeral=True)

    # ------------------------------------------------------------------
    # 1) Garantia da estrutura (categoria + 5 canais)
    # ------------------------------------------------------------------
    async def ensure_structure(self, guild: discord.Guild) -> discord.CategoryChannel:
        """Garante a categoria e os 5 canais. Preserva os existentes (busca por
        nome normalizado). Guarda os IDs em settings. Retorna a categoria."""
        # --- categoria ---
        target_cat = _normalize(config.CATEGORY_NAME)
        category: discord.CategoryChannel | None = None
        for c in guild.categories:
            if _normalize(c.name) == target_cat:
                category = c
                break
        if category is None:
            try:
                category = await guild.create_category(config.CATEGORY_NAME, reason="Estrutura do Hapination Live")
                log.info("PanelCog: categoria criada em %s: %s", guild.id, config.CATEGORY_NAME)
            except discord.Forbidden:
                log.error("PanelCog: sem permissão para criar categoria na guilda %s", guild.id)
                raise
        with get_db() as con:
            set_setting(con, "category_hapination", str(category.id))

        # --- canais ---
        for key, name in config.CHANNELS.items():
            target = _normalize(name)
            channel: discord.TextChannel | None = None
            for c in category.channels:
                if isinstance(c, discord.TextChannel) and _normalize(c.name) == target:
                    channel = c
                    break
            if channel is None:
                # fallback: procura em toda a guilda (canal fora da categoria)
                for c in guild.text_channels:
                    if _normalize(c.name) == target:
                        channel = c
                        break
            if channel is None:
                overwrites = await self._channel_overwrites(guild, key)
                try:
                    channel = await guild.create_text_channel(
                        name, category=category, overwrites=overwrites, reason="Canal do Hapination Live"
                    )
                    log.info("PanelCog: canal criado em %s: %s", guild.id, name)
                except discord.Forbidden:
                    log.error("PanelCog: sem permissão para criar o canal %s na guilda %s", name, guild.id)
                    continue
            else:
                # garante que está dentro da categoria correta
                if channel.category_id != category.id:
                    try:
                        await channel.edit(category=category)
                    except Exception:
                        log.warning("PanelCog: não foi possível mover %s para a categoria", channel.id)
            with get_db() as con:
                set_setting(con, f"channel_{key}", str(channel.id))
        return category

    async def _channel_overwrites(self, guild: discord.Guild, channel_key: str) -> dict:
        """Monta overwrites para os 5 canais fixos.

        Regras (SPEC §2.4):
          - 📢 avisos: @everyone lê, mas só admins/proprietário escrevem.
          - 🔒 administracao: só admins/proprietário leem (@everyone negado).
          - demais: padrão (everyone lê e escreve).
        """
        everyone_view = True
        everyone_send = True
        if channel_key == "avisos":
            everyone_send = False
        elif channel_key == "administracao":
            everyone_view = False
            everyone_send = False

        ow: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=everyone_view,
                send_messages=everyone_send,
                read_message_history=everyone_view,
            )
        }
        if guild.me is not None:
            ow[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                read_message_history=True, embed_links=True, attach_files=True,
            )
        for member in await self._staff_members(guild):
            ow[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True, read_message_history=True,
            )
        return ow

    def _staff_ids(self) -> list[int]:
        ids: set[int] = set()
        if config.OWNER_DISCORD_ID:
            ids.add(config.OWNER_DISCORD_ID)
        try:
            with get_db() as con:
                for row in con.execute("SELECT user_id FROM admins"):
                    ids.add(int(row["user_id"]))
        except Exception:
            log.exception("PanelCog: erro ao listar admins")
        return sorted(ids)

    async def _staff_members(self, guild: discord.Guild) -> list[discord.Member]:
        members: list[discord.Member] = []
        for uid in self._staff_ids():
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    continue
            if member is not None:
                members.append(member)
        return members

    # ------------------------------------------------------------------
    # 2) Painel público (mensagem fixa em 🎛️painel)
    # ------------------------------------------------------------------
    def _read_setting(self, key: str) -> str | None:
        with get_db() as con:
            return get_setting(con, key, None)

    def _get_channel(self, guild: discord.Guild, key: str) -> discord.TextChannel | None:
        raw = self._read_setting(f"channel_{key}")
        if raw:
            try:
                ch = guild.get_channel(int(raw))
                if isinstance(ch, discord.TextChannel):
                    return ch
            except ValueError:
                pass
        # fallback por nome normalizado
        target = _normalize(config.CHANNELS.get(key, ""))
        for c in guild.text_channels:
            if _normalize(c.name) == target:
                return c
        return None

    async def ensure_public_panel(self, guild: discord.Guild) -> None:
        """Garante a mensagem fixa do painel público. Se já existir (settings
        message_painel_id), reutiliza — nunca duplica."""
        channel = self._get_channel(guild, "painel")
        if channel is None:
            log.warning("PanelCog: canal de painel ausente na guilda %s", guild.id)
            return
        msg_id = self._read_setting("message_painel_id")
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                if not msg.pinned:
                    try:
                        await msg.pin()
                    except Exception:
                        pass
                return  # já existe — reutilizar
            except (discord.NotFound, ValueError):
                pass  # mensagem apagada -> reposta abaixo
            except discord.HTTPException:
                return
        try:
            await self._post_public_panel(channel)
        except Exception:
            log.exception("PanelCog: erro ao postar painel público em %s", channel.id)

    async def _post_public_panel(self, channel: discord.TextChannel) -> discord.Message:
        emb = embed(
            title="🎬 Hapination Live — Painel",
            description=(
                "Bem-vindo ao **Hapination Live**!\n\n"
                "Clique no botão abaixo para abrir seu **painel privado** e gerenciar:\n"
                "▶️ sessões simuladas • 📹 vídeos • 💰 créditos fictícios • 📜 histórico\n\n"
                "Seus dados (saldo, arquivos e histórico) nunca são expostos em canais públicos."
            ),
            color=config.COLOR_PRIMARY,
        )
        file: discord.File | None = None
        image_path = config.BASE_DIR / "painel_tiktok.jpg"
        if image_path.exists():
            try:
                file = discord.File(str(image_path), filename="painel_tiktok.jpg")
                emb.set_image(url="attachment://painel_tiktok.jpg")
            except Exception:
                log.exception("PanelCog: não foi possível anexar painel_tiktok.jpg")
                file = None
        msg = await channel.send(embed=emb, view=PublicPanelView(self), file=file)
        try:
            await msg.pin()
            # remove a mensagem de sistema "X fixou uma mensagem"
            async for m in channel.history(limit=5):
                if m.type == discord.MessageType.pins_add and m.author == self.bot.user:
                    try:
                        await m.delete()
                    except Exception:
                        pass
        except Exception:
            log.warning("PanelCog: não foi possível fixar a mensagem do painel")
        with get_db() as con:
            set_setting(con, "message_painel_id", str(msg.id))
        return msg

    # ------------------------------------------------------------------
    # 3) Painel privado individual (canal 👤・painel-<id>)
    # ------------------------------------------------------------------
    async def open_user_panel(self, interaction: discord.Interaction) -> None:
        """Callback do botão "🎟️ Abrir meu painel"."""
        try:
            # cooldown 10s (responde ephemeral se bloqueado)
            if not await check_cooldown(interaction, "open_panel"):
                return
            # suspensão bloqueia a abertura do painel
            if is_suspended(interaction.user.id):
                await interaction.response.send_message(
                    embed=error_embed(
                        "Conta suspensa",
                        "Sua conta está suspensa e você não pode abrir o painel. "
                        "Procure o proprietário para mais informações.",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)

            guild = interaction.guild
            if guild is None:
                await interaction.followup.send(embed=error_embed("Indisponível", "Use o painel dentro de uma guilda."), ephemeral=True)
                return

            category = await self.ensure_structure(guild)
            user = interaction.user
            expected_name = f"👤・painel-{user.id}"
            target = _normalize(expected_name)

            existing: discord.TextChannel | None = None
            for c in category.text_channels:
                if _normalize(c.name) == target:
                    existing = c
                    break

            if existing is not None:
                await interaction.followup.send(
                    embed=info_embed("Painel já aberto", f"Seu painel privado já está aberto em {existing.mention}."),
                    ephemeral=True,
                )
                return

            overwrites = await self._private_panel_overwrites(guild, user)
            channel: discord.TextChannel
            try:
                channel = await guild.create_text_channel(
                    expected_name, category=category, overwrites=overwrites, reason="Painel privado do usuário"
                )
            except discord.HTTPException:
                # fallback de nome (alguns caracteres podem ser recusados)
                channel = await guild.create_text_channel(
                    f"painel-{user.id}", category=category, overwrites=overwrites, reason="Painel privado do usuário"
                )

            emb = await self._build_private_panel_embed(user)
            await channel.send(content=user.mention, embed=emb, view=PrivatePanelView(self))
            await interaction.followup.send(
                embed=success_embed("Painel aberto", f"Seu painel privado foi criado em {channel.mention}."),
                ephemeral=True,
            )
        except Exception:
            log.exception("PanelCog: erro ao abrir painel para %s", getattr(interaction.user, "id", "?"))
            await self._safe_error(interaction, "Não foi possível abrir seu painel. Tente novamente em instantes.")

    async def _private_panel_overwrites(
        self, guild: discord.Guild, user: discord.Member
    ) -> dict:
        """Overwrites do canal privado:
        - @everyone: negado (não vê).
        - usuário: ver + escrever + ler histórico.
        - proprietário e admins: ver + ler histórico + escrever (apoio).
        - bot: permissões completas no canal.
        """
        ow: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, attach_files=True,
            ),
        }
        if guild.me is not None:
            ow[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                read_message_history=True, embed_links=True, attach_files=True, manage_channels=True,
            )
        for member in await self._staff_members(guild):
            ow[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
        return ow

    async def _build_private_panel_embed(self, user: discord.abc.User) -> discord.Embed:
        """Monta o embed do painel privado com saldo, sessões ativas, vídeos,
        histórico e tabela de preços. Dados privados — só postados no canal
        privado do usuário."""
        with get_db() as con:
            balance = services.get_balance(con, user.id)
            durations = services.list_durations(con)
            maintenance = services.maintenance_on(con)
            maintenance_msg = services.maintenance_message(con) if maintenance else ""

        desc_lines: list[str] = []
        if maintenance:
            desc_lines.append(f"⚠️ **Manutenção em andamento**: {maintenance_msg} — novas sessões e uploads estão temporariamente bloqueados.")
        desc_lines.append(f"Olá, **{getattr(user, 'display_name', user.name)}**! Este é o seu painel privado. Use os botões abaixo.")

        emb = embed(
            title="👤 Meu Painel — Hapination Live",
            description="\n".join(desc_lines),
            color=config.COLOR_PRIMARY,
        )
        emb.add_field(name="💰 Saldo atual", value=money(balance), inline=True)

        # --- sessões ativas (delega para SessionsPrices; trata None) ---
        sessions_cog = self._get_sessions_cog()
        active: list[dict] | None = None
        if sessions_cog is not None and hasattr(sessions_cog, "active_summary"):
            try:
                active = await sessions_cog.active_summary(user.id)
            except Exception:
                log.exception("PanelCog: active_summary falhou")
                active = None
        emb.add_field(name="▶️ Sessões simuladas ativas", value=self._fmt_active(active), inline=False)

        # --- vídeos cadastrados (delega para VideosCog) ---
        videos_cog = self._get_videos_cog()
        videos: list[dict] | None = None
        if videos_cog is not None and hasattr(videos_cog, "list_summary"):
            try:
                videos = await videos_cog.list_summary(user.id)
            except Exception:
                log.exception("PanelCog: list_summary falhou")
                videos = None
        emb.add_field(name="🎬 Vídeos cadastrados", value=self._fmt_videos(videos), inline=False)

        # --- histórico resumido ---
        history: list[dict] | None = None
        if sessions_cog is not None and hasattr(sessions_cog, "history_summary"):
            try:
                history = await sessions_cog.history_summary(user.id, limit=5)
            except Exception:
                log.exception("PanelCog: history_summary falhou")
                history = None
        emb.add_field(name="📜 Histórico recente", value=self._fmt_history(history), inline=False)

        # --- tabela de durações/preços ---
        emb.add_field(name="💲 Durações e preços", value=self._fmt_prices(durations), inline=False)
        return emb

    # -- formatadores defensivos (chaves exatas dos outros cogs desconhecidas) --
    @staticmethod
    def _fmt_active(items: list[dict] | None) -> str:
        if items is None:
            return "Indisponível no momento (módulo de sessões não carregado)."
        if not items:
            return "Nenhuma sessão simulada ativa."
        lines = []
        for it in items[:5]:
            sid = _pick(it, "id", "session_id", default="?")
            vid = _pick(it, "video", "filename", "video_id", default="?")
            exp = _pick(it, "expires_at", "expira", "expires", default="?")
            cons = _pick(it, "consumido", "consumed", "billed_cents", default=None)
            cons_s = money(int(cons)) if isinstance(cons, (int, float)) else (cons if cons not in (None, "") else "?")
            lines.append(f"• `#{sid}` • {vid} • expira: {exp} • consumido: {cons_s}")
        if len(items) > 5:
            lines.append(f"… +{len(items) - 5} sessão(ões)")
        return "\n".join(lines)[:1000]

    @staticmethod
    def _fmt_videos(items: list[dict] | None) -> str:
        if items is None:
            return "Indisponível no momento (módulo de vídeos não carregado)."
        if not items:
            return "Nenhum vídeo cadastrado. Use 📤 Enviar vídeo."
        lines = [f"**{len(items)}** vídeo(s) cadastrado(s):"]
        for it in items[:4]:
            name = _pick(it, "filename", "name", "video", default="vídeo")
            size = _pick(it, "size_bytes", "size", default=None)
            size_s = f" ({int(size) / 1048576:.1f}MB)" if isinstance(size, (int, float)) else ""
            lines.append(f"• {name}{size_s}")
        if len(items) > 4:
            lines.append(f"… +{len(items) - 4}")
        return "\n".join(lines)[:1000]

    @staticmethod
    def _fmt_history(items: list[dict] | None) -> str:
        if items is None:
            return "Indisponível no momento."
        if not items:
            return "Sem histórico registrado ainda."
        lines = []
        for it in items[:5]:
            sid = _pick(it, "id", "session_id", default="?")
            status = _pick(it, "status", default="?")
            dur = _pick(it, "duration_hours", "duracao", default="?")
            billed = _pick(it, "billed_cents", "consumido", default=None)
            billed_s = money(int(billed)) if isinstance(billed, (int, float)) else (billed if billed not in (None, "") else "?")
            lines.append(f"• `#{sid}` • {status} • {dur}h • {billed_s}")
        return "\n".join(lines)[:1000]

    @staticmethod
    def _fmt_prices(durations) -> str:
        if not durations:
            return "Nenhuma duração cadastrada."
        lines = []
        for row in durations:
            try:
                hours = row["hours"]
                price = row["price_cents"]
                lines.append(f"• **{hours}h** → {money(int(price))}")
            except Exception:
                continue
        return "\n".join(lines)[:1000] or "Nenhuma duração cadastrada."

    # ------------------------------------------------------------------
    # 4) Ações dos botões do painel privado (execução direta)
    # ------------------------------------------------------------------
    def _get_sessions_cog(self):
        for name in ("SessionsPrices", "SessionsPricesCog"):
            cog = self.bot.get_cog(name)
            if cog is not None:
                return cog
        return None

    def _get_videos_cog(self):
        for name in ("VideosCog", "Videos"):
            cog = self.bot.get_cog(name)
            if cog is not None:
                return cog
        return None

    def _maintenance_state(self) -> tuple[bool, str]:
        try:
            with get_db() as con:
                on = services.maintenance_on(con)
                msg = services.maintenance_message(con) if on else ""
                return on, msg
        except Exception:
            log.exception("PanelCog: erro ao ler estado de manutenção")
            return False, ""

    async def _action_allowed(self, interaction: discord.Interaction, *, maintenance_block: bool) -> bool:
        """Verifica suspensão (sempre) e manutenção (se maintenance_block).
        Responde ephemeral amigável e retorna False se bloqueado."""
        if is_suspended(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Conta suspensa", "Sua conta está suspensa. Procure o proprietário."),
                ephemeral=True,
            )
            return False
        if maintenance_block:
            on, msg = self._maintenance_state()
            if on:
                await interaction.response.send_message(
                    embed=warn_embed("Manutenção em andamento", msg),
                    ephemeral=True,
                )
                return False
        return True

    async def _safe_error(self, interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed("Erro", text), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed("Erro", text), ephemeral=True)
        except Exception:
            log.exception("PanelCog: falha ao reportar erro ao usuário")

    # -- 📤 Enviar vídeo -> VideosCog.enable_upload_mode ----------------
    async def action_enable_upload(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._action_allowed(interaction, maintenance_block=True):
                return
            cog = self._get_videos_cog()
            if cog is None or not hasattr(cog, "enable_upload_mode"):
                await interaction.response.send_message(
                    embed=error_embed("Indisponível", "O módulo de vídeos não está disponível no momento."),
                    ephemeral=True,
                )
                return
            await cog.enable_upload_mode(interaction)  # o fluxo responde a interação
        except Exception:
            log.exception("PanelCog: erro em action_enable_upload")
            await self._safe_error(interaction, "Não foi possível ativar o envio de vídeo.")

    # -- ▶️ Iniciar sessão -> SessionsPrices.start_flow ------------------
    async def action_start_session(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._action_allowed(interaction, maintenance_block=True):
                return
            cog = self._get_sessions_cog()
            if cog is None or not hasattr(cog, "start_flow"):
                await interaction.response.send_message(
                    embed=error_embed("Indisponível", "O módulo de sessões não está disponível no momento."),
                    ephemeral=True,
                )
                return
            await cog.start_flow(interaction)
        except Exception:
            log.exception("PanelCog: erro em action_start_session")
            await self._safe_error(interaction, "Não foi possível iniciar a sessão.")

    # -- 🔄 Renovar -> SessionsPrices.renew_flow -------------------------
    async def action_renew_session(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._action_allowed(interaction, maintenance_block=True):
                return
            cog = self._get_sessions_cog()
            if cog is None or not hasattr(cog, "renew_flow"):
                await interaction.response.send_message(
                    embed=error_embed("Indisponível", "O módulo de sessões não está disponível no momento."),
                    ephemeral=True,
                )
                return
            await cog.renew_flow(interaction)
        except Exception:
            log.exception("PanelCog: erro em action_renew_session")
            await self._safe_error(interaction, "Não foi possível renovar a sessão.")

    # -- ⏹️ Encerrar -> SessionsPrices.end_flow (permitido na manutenção) -
    async def action_end_session(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._action_allowed(interaction, maintenance_block=False):
                return
            cog = self._get_sessions_cog()
            if cog is None or not hasattr(cog, "end_flow"):
                await interaction.response.send_message(
                    embed=error_embed("Indisponível", "O módulo de sessões não está disponível no momento."),
                    ephemeral=True,
                )
                return
            await cog.end_flow(interaction)
        except Exception:
            log.exception("PanelCog: erro em action_end_session")
            await self._safe_error(interaction, "Não foi possível encerrar a sessão.")

    # -- 🎬 Meus vídeos -> lista ephemeral --------------------------------
    async def action_list_videos(self, interaction: discord.Interaction) -> None:
        try:
            if is_suspended(interaction.user.id):
                await interaction.response.send_message(
                    embed=error_embed("Conta suspensa", "Sua conta está suspensa."), ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            cog = self._get_videos_cog()
            if cog is None or not hasattr(cog, "list_summary"):
                await interaction.followup.send(
                    embed=error_embed("Indisponível", "O módulo de vídeos não está disponível no momento."), ephemeral=True
                )
                return
            items = await cog.list_summary(interaction.user.id)
            emb = embed(title="🎬 Meus vídeos", description=self._fmt_videos(items), color=config.COLOR_INFO)
            await interaction.followup.send(embed=emb, ephemeral=True)
        except Exception:
            log.exception("PanelCog: erro em action_list_videos")
            await self._safe_error(interaction, "Não foi possível listar seus vídeos.")

    # -- 📜 Histórico -> ephemeral ----------------------------------------
    async def action_history(self, interaction: discord.Interaction) -> None:
        try:
            if is_suspended(interaction.user.id):
                await interaction.response.send_message(
                    embed=error_embed("Conta suspensa", "Sua conta está suspensa."), ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            cog = self._get_sessions_cog()
            if cog is None or not hasattr(cog, "history_summary"):
                await interaction.followup.send(
                    embed=error_embed("Indisponível", "O módulo de sessões não está disponível no momento."), ephemeral=True
                )
                return
            items = await cog.history_summary(interaction.user.id, limit=10)
            emb = embed(title="📜 Histórico de sessões", description=self._fmt_history(items), color=config.COLOR_INFO)
            await interaction.followup.send(embed=emb, ephemeral=True)
        except Exception:
            log.exception("PanelCog: erro em action_history")
            await self._safe_error(interaction, "Não foi possível carregar seu histórico.")

    # -- 💲 Preços -> tabela ephemeral ------------------------------------
    async def action_prices(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            with get_db() as con:
                durations = services.list_durations(con)
            emb = embed(
                title="💲 Tabela de durações e preços",
                description=self._fmt_prices(durations) + "\n\nCréditos fictícios • sessões simuladas.",
                color=config.COLOR_INFO,
            )
            await interaction.followup.send(embed=emb, ephemeral=True)
        except Exception:
            log.exception("PanelCog: erro em action_prices")
            await self._safe_error(interaction, "Não foi possível carregar a tabela de preços.")

    # -- ❌ Fechar painel -> confirmação e exclusão do canal --------------
    def _is_user_panel_channel(self, interaction: discord.Interaction) -> bool:
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return False
        expected = _normalize(f"👤・painel-{interaction.user.id}")
        return _normalize(ch.name) == expected

    async def action_close_panel(self, interaction: discord.Interaction) -> None:
        try:
            if not self._is_user_panel_channel(interaction):
                await interaction.response.send_message(
                    embed=error_embed("Não permitido", "Este botão só funciona no seu próprio canal de painel."),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=warn_embed(
                    "Fechar painel?",
                    "Tem certeza? O canal será **excluído**. Você poderá reabrir seu painel a qualquer momento pelo botão no 🎛️painel público.",
                ),
                view=ConfirmCloseView(self),
                ephemeral=True,
            )
        except Exception:
            log.exception("PanelCog: erro em action_close_panel")
            await self._safe_error(interaction, "Não foi possível solicitar o fechamento.")

    async def confirm_close_panel(self, interaction: discord.Interaction) -> None:
        """Callback da confirmação: edita a mensagem e exclui o canal."""
        try:
            if not self._is_user_panel_channel(interaction):
                await interaction.response.edit_message(
                    embed=error_embed("Não permitido", "Só é possível fechar o seu próprio canal de painel."),
                    view=None,
                )
                return
            channel = interaction.channel
            assert isinstance(channel, discord.TextChannel)
            await interaction.response.edit_message(
                embed=success_embed("Fechando painel", "Seu canal de painel será excluído agora."),
                view=None,
            )
            await asyncio.sleep(0.5)
            await channel.delete(reason="Painel fechado pelo usuário")
        except Exception:
            log.exception("PanelCog: erro ao confirmar fechamento do painel")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        embed=error_embed("Erro", "Não foi possível excluir o canal. Tente manualmente."),
                        ephemeral=True,
                    )
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
