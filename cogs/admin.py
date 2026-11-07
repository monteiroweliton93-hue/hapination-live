"""cogs/admin.py — AdminCog do Hapination Live.

Painel administrativo fixo em 🔒・administracao (embed preto+roxo com botões
persistentes) e comandos do grupo /admin: usuário (somente leitura), sessões,
vídeos, relatório (dados SIMULADOS), anúncios e manutenção.

REGRA DE NEGÓCIO: administrador gerencia TUDO exceto CRÉDITOS. Qualquer ação
que tocar créditos é bloqueada com erro específico — só o proprietário altera
créditos. Suspensão/reativação, gestão de admins e créditos ficam no
UsersCreditsCog; este cog oferece apenas atalhos (botões) que chamam aquele
cog, retornando erro amigável se ele não estiver carregado.

Núcleo (core/) já escrito — este arquivo NÃO duplica SQL de leitura
compartilhada nem matemática de cobrança (usa core.services).
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import timedelta
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.common import (
    check_cooldown,
    embed,
    success_embed,
    warn_embed,
    error_embed,
    info_embed,
    money,
    fmt_duration,
    is_owner,
    is_admin,
    admin_only,
    owner_only,
    log,
    notify_user,
)
from core.database import get_db, get_setting, set_setting, ensure_user
from core.services import (
    get_balance,
    active_sessions,
    maintenance_on,
    maintenance_message,
    parse_iso,
    now_utc,
)

# Itens por página nas listagens paginadas.
PAGE_SIZE = 15
# Ação de cooldown genérica para comandos administrativos.
COOLDOWN_ADMIN = "admin_action"


# ---------------------------------------------------------------------------
# Utilitários de formatação
# ---------------------------------------------------------------------------
def fmt_bytes(n: int | None) -> str:
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        f /= 1024.0
        if f < 1024:
            return f"{f:.1f} {unit}"
    return f"{f:.1f} PB"


def ts_discord(val: str | None) -> str:
    """Converte data ISO do banco em timestamp legível do Discord."""
    dt = parse_iso(val)
    if dt is None:
        return "—"
    return f"<t:{int(dt.timestamp())}:f>"


def chunk_lines(lines: list[str], max_chars: int = 1800) -> list[str]:
    """Agrupa linhas em páginas respeitando o limite de caracteres."""
    pages: list[str] = []
    cur = ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > max_chars:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pages.append(cur)
    return pages or ["—"]


async def reply(interaction: discord.Interaction, **kwargs: Any) -> None:
    """Responde usando response ou followup (se já houve defer)."""
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


# ---------------------------------------------------------------------------
# Paginação simples (não persistente)
# ---------------------------------------------------------------------------
class SimplePaginator(discord.ui.View):
    """View com botões ◀ ▶ para paginar listagens. Timeout curto, sem custom_id."""

    def __init__(self, title: str, pages: list[str], color: int = config.COLOR_PRIMARY):
        super().__init__(timeout=120)
        self.title = title
        self.pages = pages
        self.color = color
        self.page = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= len(self.pages) - 1

    def build_embed(self) -> discord.Embed:
        e = embed(self.title, self.pages[self.page], self.color)
        e.set_footer(text=f"Página {self.page + 1}/{len(self.pages)} • {config.FOOTER_TEXT}")
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.page < len(self.pages) - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            self.message  # type: ignore[attr-defined]
            if getattr(self, "message", None):
                await self.message.edit(view=self)  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Modal genérico para ações administrativas (aberto pelos botões persistentes)
# ---------------------------------------------------------------------------
class AdminActionModal(discord.ui.Modal):
    """Modal flexível: até 5 campos de texto. Despacha para o cog por `action`."""

    def __init__(
        self,
        cog: "AdminCog",
        action: str,
        title: str,
        fields: list[tuple[str, str, bool, discord.TextStyle, int]],
    ) -> None:
        super().__init__(title=title, timeout=None)
        self.cog = cog
        self.action = action
        self._inputs: list[discord.ui.TextInput] = []
        for label, placeholder, required, style, max_len in fields:
            ti = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                required=required,
                style=style,
                max_length=max_len,
            )
            self.add_item(ti)
            self._inputs.append(ti)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = [ti.value.strip() for ti in self._inputs]
        await self.cog.handle_modal(interaction, self.action, values)


# ---------------------------------------------------------------------------
# View persistente do painel administrativo (sobrevive a reinícios)
# ---------------------------------------------------------------------------
class AdminPanelView(discord.ui.View):
    """Botões persistentes do painel administrativo. custom_id com prefixo hap_."""

    def __init__(self, cog: "AdminCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _admin_ok(self, interaction: discord.Interaction, action: str) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Este painel é restrito à equipe administrativa."),
                ephemeral=True,
            )
            return False
        if not await check_cooldown(interaction, f"{COOLDOWN_ADMIN}_{action}"):
            return False
        return True

    async def _open_modal(self, interaction: discord.Interaction, modal: AdminActionModal) -> None:
        try:
            await interaction.response.send_modal(modal)
        except Exception as exc:
            log.exception("Falha ao abrir modal %s: %s", modal.action, exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível abrir o formulário."))

    # --- Linha 1: consultas (leitura) ---
    @discord.ui.button(label="👤 Ver usuário", style=discord.ButtonStyle.primary, custom_id="hap_admin_ver_usuario", row=0)
    async def btn_ver_usuario(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "ver_usuario"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "ver_usuario", "Ver usuário",
            [("ID do usuário", "Ex: 123456789012345678", True, discord.TextStyle.short, 32)],
        ))

    @discord.ui.button(label="📋 Usuários", style=discord.ButtonStyle.secondary, custom_id="hap_admin_usuarios", row=0)
    async def btn_usuarios(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "usuarios"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.list_users(interaction)

    @discord.ui.button(label="▶️ Sessões ativas", style=discord.ButtonStyle.secondary, custom_id="hap_admin_sessoes", row=0)
    async def btn_sessoes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "sessoes"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.list_active_sessions(interaction)

    @discord.ui.button(label="🎬 Vídeos", style=discord.ButtonStyle.secondary, custom_id="hap_admin_videos", row=0)
    async def btn_videos(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "videos"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.list_videos(interaction)

    @discord.ui.button(label="📊 Relatório 7d", style=discord.ButtonStyle.secondary, custom_id="hap_admin_relatorio", row=0)
    async def btn_relatorio(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "relatorio"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.report(interaction, 7)

    # --- Linha 2: anúncio / manutenção / atualizar ---
    @discord.ui.button(label="📢 Anunciar", style=discord.ButtonStyle.success, custom_id="hap_admin_anunciar", row=1)
    async def btn_anunciar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "anunciar"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "anunciar", "Novo anúncio",
            [("Texto do anúncio", "Mensagem que será enviada em 📢・avisos", True, discord.TextStyle.paragraph, 2000)],
        ))

    @discord.ui.button(label="🛠️ Manutenção ON", style=discord.ButtonStyle.danger, custom_id="hap_admin_maint_on", row=1)
    async def btn_maint_on(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "maint_on"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "manutencao_ligar", "Ativar manutenção",
            [("Mensagem (opcional)", "Motivo/aviso aos usuários", False, discord.TextStyle.paragraph, 500)],
        ))

    @discord.ui.button(label="🛑 Manutenção OFF", style=discord.ButtonStyle.danger, custom_id="hap_admin_maint_off", row=1)
    async def btn_maint_off(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Desligar a manutenção é exclusivo do proprietário."),
                ephemeral=True,
            )
            return
        if not await check_cooldown(interaction, f"{COOLDOWN_ADMIN}_maint_off"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.maintenance_off(interaction)

    @discord.ui.button(label="🔄 Atualizar painel", style=discord.ButtonStyle.secondary, custom_id="hap_admin_refresh", row=1)
    async def btn_refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "refresh"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.ensure_panel(force=True)
        await reply(interaction, embed=success_embed("Painel atualizado", "A mensagem fixa foi atualizada."))

    # --- Linha 3: atalhos que delegam para outros cogs / ações diretas ---
    @discord.ui.button(label="⛔ Suspender", style=discord.ButtonStyle.danger, custom_id="hap_admin_suspender", row=2)
    async def btn_suspender(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Suspender usuários é exclusivo do proprietário (UsersCreditsCog)."),
                ephemeral=True,
            )
            return
        if not await check_cooldown(interaction, f"{COOLDOWN_ADMIN}_suspender"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "suspender", "Suspender usuário (atalho → UsersCreditsCog)",
            [
                ("ID do usuário", "ID do Discord", True, discord.TextStyle.short, 32),
                ("Motivo (opcional)", "", False, discord.TextStyle.short, 200),
            ],
        ))

    @discord.ui.button(label="✅ Reativar", style=discord.ButtonStyle.success, custom_id="hap_admin_reativar", row=2)
    async def btn_reativar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Reativar usuários é exclusivo do proprietário (UsersCreditsCog)."),
                ephemeral=True,
            )
            return
        if not await check_cooldown(interaction, f"{COOLDOWN_ADMIN}_reativar"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "reativar", "Reativar usuário (atalho → UsersCreditsCog)",
            [("ID do usuário", "ID do Discord", True, discord.TextStyle.short, 32)],
        ))

    @discord.ui.button(label="✏️ Apelido", style=discord.ButtonStyle.secondary, custom_id="hap_admin_apelido", row=2)
    async def btn_apelido(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "apelido"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "apelido", "Alterar apelido (atalho → UsersCreditsCog)",
            [
                ("ID do usuário", "ID do Discord", True, discord.TextStyle.short, 32),
                ("Novo apelido", "3–20 caracteres", True, discord.TextStyle.short, 20),
            ],
        ))

    @discord.ui.button(label="⏹️ Encerrar sessão", style=discord.ButtonStyle.danger, custom_id="hap_admin_encerrar", row=2)
    async def btn_encerrar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "encerrar"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "encerrar_sessao", "Encerrar sessão forçadamente",
            [("ID da sessão", "Número da sessão", True, discord.TextStyle.short, 16)],
        ))

    @discord.ui.button(label="🗑️ Excluir vídeo", style=discord.ButtonStyle.danger, custom_id="hap_admin_excluir_video", row=2)
    async def btn_excluir_video(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._admin_ok(interaction, "excluir_video"):
            return
        await self._open_modal(interaction, AdminActionModal(
            self.cog, "excluir_video", "Excluir vídeo",
            [("ID do vídeo", "Número do vídeo", True, discord.TextStyle.short, 16)],
        ))


# ---------------------------------------------------------------------------
# Cog principal
# ---------------------------------------------------------------------------
class AdminCog(commands.Cog):
    """Painel administrativo e comandos /admin do Hapination Live."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._start_time = discord.utils.utcnow()
        self._panel_task: Any = None
        self._panel_lock = False

        # Grupo /admin é COMPARTILHADO com o UsersCreditsCog (que carrega antes
        # e define os subgrupos creditos/admin). Fazemos get-or-create para não
        # duplicar e nem quebrar o carregamento deste cog.
        self.admin_group: app_commands.Group = self.bot.tree.get_command("admin")  # type: ignore[assignment]
        if self.admin_group is None:
            self.admin_group = app_commands.Group(
                name="admin", description="Comandos administrativos do Hapination Live."
            )
            self.bot.tree.add_command(self.admin_group)

        self.usuario_group = self._get_or_create_subgroup("usuario", "Gestão de usuários (somente leitura para admins).")
        self.sessoes_group = self._get_or_create_subgroup("sessoes", "Gestão de sessões simuladas.")
        self.videos_group = self._get_or_create_subgroup("videos", "Gestão de vídeos armazenados.")
        self.manutencao_group = self._get_or_create_subgroup("manutencao", "Modo de manutenção do bot.")

        # Registra os comandos deste cog (remove homônimos para evitar conflito).
        self._register(self.usuario_group, "ver", "Ver detalhes de um usuário (somente leitura).", self.cmd_usuario_ver)
        self._register(self.usuario_group, "listar", "Listar usuários cadastrados (últimos).", self.cmd_usuario_listar)
        self._register(self.sessoes_group, "ativas", "Listar todas as sessões simuladas ativas.", self.cmd_sessoes_ativas)
        self._register(self.sessoes_group, "encerrar", "Encerrar forçadamente uma sessão por ID.", self.cmd_sessoes_encerrar)
        self._register(self.videos_group, "listar", "Listar todos os vídeos armazenados.", self.cmd_videos_listar)
        self._register(self.videos_group, "excluir", "Excluir um vídeo por ID.", self.cmd_videos_excluir)
        self._register(self.admin_group, "relatorio", "Relatório administrativo (dados simulados).", self.cmd_relatorio)
        self._register(self.admin_group, "anunciar", "Enviar anúncio para 📢・avisos.", self.cmd_anunciar)
        self._register(self.manutencao_group, "ligar", "Ativar modo de manutenção.", self.cmd_manutencao_ligar)
        self._register(self.manutencao_group, "desligar", "Desativar manutenção (só proprietário).", self.cmd_manutencao_desligar)

    # ----- Registro de grupos/comandos -----
    def _get_or_create_subgroup(self, name: str, description: str) -> app_commands.Group:
        existing = self.admin_group.get_command(name)
        if isinstance(existing, app_commands.Group):
            return existing
        if existing is not None:
            # Comando com mesmo nome — remove para ceder lugar ao subgrupo.
            try:
                self.admin_group.remove_command(name)
            except Exception:
                pass
        return app_commands.Group(name=name, description=description, parent=self.admin_group)

    def _register(self, group: app_commands.Group, name: str, description: str, callback: Callable[..., Any]) -> None:
        if group.get_command(name) is not None:
            try:
                group.remove_command(name)
            except Exception:
                pass
        try:
            group.command(name=name, description=description)(callback)
        except Exception:
            log.exception("Falha ao registrar comando /admin %s %s", group.name, name)

    # ----- Ciclo de vida -----
    async def cog_load(self) -> None:
        # Registra a view persistente para sobreviver a reinícios.
        self.bot.add_view(AdminPanelView(self))
        log.info("AdminCog: view persistente do painel administrativo registrada.")

    async def cog_unload(self) -> None:
        if self._panel_task is not None:
            self._panel_task.cancel()

    @commands.Cog.listener("on_ready")
    async def _on_ready_ensure_panel(self) -> None:
        if self._panel_lock:
            return
        self._panel_lock = True
        try:
            await self.ensure_panel()
        except Exception as exc:
            log.exception("AdminCog: falha ao garantir painel no on_ready: %s", exc)
        finally:
            self._panel_lock = False

    # ----- Canais -----
    def find_channel(self, name: str) -> discord.TextChannel | None:
        """Busca canal por nome exato, depois por sufixo normalizado."""
        suffix = name.split("・")[-1] if "・" in name else name
        for guild in self.bot.guilds:
            for ch in guild.text_channels:
                if ch.name == name or ch.name.endswith(suffix) or suffix in ch.name:
                    return ch
        return None

    async def find_or_create_admin_channel(self) -> discord.TextChannel | None:
        ch = self.find_channel(config.ADMIN_CHANNEL_NAME)
        if ch is not None:
            return ch
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return None
        category = discord.utils.get(guild.categories, name=config.CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(config.CATEGORY_NAME)
            except Exception as exc:
                log.warning("AdminCog: não foi possível criar categoria: %s", exc)
                category = None
        overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
        if config.OWNER_DISCORD_ID:
            owner_member = guild.get_member(config.OWNER_DISCORD_ID)
            if owner_member is not None:
                overwrites[owner_member] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_messages=True
                )
        try:
            return await guild.create_text_channel(
                config.ADMIN_CHANNEL_NAME, category=category, overwrites=overwrites
            )
        except Exception as exc:
            log.exception("AdminCog: não foi possível criar canal administrativo: %s", exc)
            return None

    # ----- Painel administrativo fixo -----
    def panel_embed(self) -> discord.Embed:
        with get_db() as con:
            total_users = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            active_sess = con.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE status='running'"
            ).fetchone()["c"]
            videos_row = con.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes),0) AS b FROM videos WHERE deleted=0"
            ).fetchone()
            maint = maintenance_on(con)
        uptime = discord.utils.utcnow() - self._start_time
        e = embed(
            "🛡️ Painel Administrativo — Hapination Live",
            "Gerencie usuários, sessões simuladas, vídeos, anúncios e manutenção.\n"
            "**Créditos não são alterados aqui** — só o proprietário, via UsersCreditsCog.",
            config.COLOR_PRIMARY,
        )
        e.add_field(name="🛠️ Manutenção", value="✅ ATIVA" if maint else "ℹ️ Inativa", inline=True)
        e.add_field(name="⏱️ Uptime", value=fmt_duration(uptime.total_seconds()), inline=True)
        e.add_field(name="👥 Usuários", value=str(total_users), inline=True)
        e.add_field(name="▶️ Sessões ativas", value=str(active_sess), inline=True)
        e.add_field(
            name="🎬 Vídeos",
            value=f"{videos_row['c']} ({fmt_bytes(videos_row['b'])})",
            inline=True,
        )
        e.add_field(name="📌 Como usar", value="Use os botões abaixo. Respostas são visíveis só para você.", inline=False)
        return e

    async def ensure_panel(self, force: bool = False) -> None:
        """Garante a mensagem fixa do painel em 🔒・administracao. Idempotente."""
        channel = await self.find_or_create_admin_channel()
        if channel is None:
            log.warning("AdminCog: canal administrativo indisponível — painel não enviado.")
            return

        with get_db() as con:
            msg_id_raw = get_setting(con, "admin_panel_message_id", None)
            chan_id_raw = get_setting(con, "admin_panel_channel_id", None)

        target_channel = channel
        if chan_id_raw and chan_id_raw.isdigit():
            stored = self.bot.get_channel(int(chan_id_raw))
            if isinstance(stored, discord.TextChannel):
                target_channel = stored

        msg: discord.Message | None = None
        if msg_id_raw and msg_id_raw.isdigit():
            try:
                msg = await target_channel.fetch_message(int(msg_id_raw))
            except discord.NotFound:
                msg = None
            except Exception as exc:
                log.warning("AdminCog: não foi possível buscar mensagem do painel: %s", exc)
                msg = None

        emb = self.panel_embed()
        view = AdminPanelView(self)
        try:
            if msg is not None:
                await msg.edit(embed=emb, view=view)
            else:
                sent = await target_channel.send(embed=emb, view=view)
                try:
                    await sent.pin()
                except Exception:
                    pass
                with get_db() as con:
                    set_setting(con, "admin_panel_message_id", str(sent.id))
                    set_setting(con, "admin_panel_channel_id", str(target_channel.id))
        except Exception as exc:
            log.exception("AdminCog: falha ao enviar/editar painel: %s", exc)

    # ----- Modal handler (despacha ações dos botões) -----
    async def handle_modal(self, interaction: discord.Interaction, action: str, values: list[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if action == "ver_usuario":
                uid = self._parse_int(values[0], "ID do usuário")
                await self.show_user(interaction, uid)
            elif action == "suspender":
                uid = self._parse_int(values[0], "ID do usuário")
                await self.shortcut_suspend(interaction, uid, values[1] if len(values) > 1 else "")
            elif action == "reativar":
                uid = self._parse_int(values[0], "ID do usuário")
                await self.shortcut_reactivate(interaction, uid)
            elif action == "apelido":
                uid = self._parse_int(values[0], "ID do usuário")
                await self.shortcut_nickname(interaction, uid, values[1] if len(values) > 1 else "")
            elif action == "encerrar_sessao":
                sid = self._parse_int(values[0], "ID da sessão")
                await self.force_end_session(interaction, sid)
            elif action == "excluir_video":
                vid = self._parse_int(values[0], "ID do vídeo")
                await self.delete_video(interaction, vid)
            elif action == "anunciar":
                await self.announce(interaction, values[0])
            elif action == "manutencao_ligar":
                await self.maintenance_on_action(interaction, values[0] if values and values[0] else None)
            else:
                await reply(interaction, embed=error_embed("Ação desconhecida", action))
        except _InputError as exc:
            await reply(interaction, embed=error_embed("Valor inválido", str(exc)))
        except Exception as exc:
            log.exception("AdminCog: erro em modal %s: %s", action, exc)
            await reply(interaction, embed=error_embed("Erro inesperado", "A operação falhou. Detalhes no log."))

    @staticmethod
    def _parse_int(raw: str, label: str) -> int:
        try:
            return int(raw.strip())
        except (ValueError, AttributeError):
            raise _InputError(f"{label} deve ser um número inteiro.") from None

    # ----- Atalhos para UsersCreditsCog (suspensão/reativação/apelido) -----
    async def _call_cog_method(
        self, cog_name: str, method_names: list[str], *args: Any, **kwargs: Any
    ) -> tuple[Any, str | None]:
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return None, f"O cog `{cog_name}` não está carregado no momento."
        for name in method_names:
            method = getattr(cog, name, None)
            if callable(method):
                try:
                    result = await method(*args, **kwargs)
                    return result, None
                except Exception as exc:
                    log.warning("AdminCog: %s.%s falhou: %s", cog_name, name, exc)
                    return None, f"O `{cog_name}` recusou a ação: {exc}"
        return None, (
            f"O `{cog_name}` não expõe um método para esta ação. "
            f"Use os comandos próprios dele (ex.: /admin usuario suspender)."
        )

    async def shortcut_suspend(self, interaction: discord.Interaction, user_id: int, reason: str) -> None:
        ok, err = await self._call_cog_method(
            "UsersCredits",
            ["suspend_user", "suspender_usuario", "suspend", "suspender"],
            interaction, user_id, reason,
        )
        if err:
            await reply(interaction, embed=warn_embed("Atalho indisponível", err))
        else:
            await reply(interaction, embed=success_embed("Usuário suspenso", f"ID: `{user_id}` (via UsersCreditsCog)."))

    async def shortcut_reactivate(self, interaction: discord.Interaction, user_id: int) -> None:
        ok, err = await self._call_cog_method(
            "UsersCredits",
            ["reactivate_user", "reativar_usuario", "unsuspend", "reactivate", "reativar"],
            interaction, user_id,
        )
        if err:
            await reply(interaction, embed=warn_embed("Atalho indisponível", err))
        else:
            await reply(interaction, embed=success_embed("Usuário reativado", f"ID: `{user_id}` (via UsersCreditsCog)."))

    async def shortcut_nickname(self, interaction: discord.Interaction, user_id: int, nickname: str) -> None:
        ok, err = await self._call_cog_method(
            "UsersCredits",
            ["set_nickname", "definir_apelido", "change_nickname", "alterar_apelido"],
            interaction, user_id, nickname,
        )
        if err:
            await reply(interaction, embed=warn_embed("Atalho indisponível", err))
        else:
            await reply(interaction, embed=success_embed("Apelido alterado", f"ID: `{user_id}` → **{nickname}** (via UsersCreditsCog)."))

    # =======================================================================
    # 1. Gestão de usuários (SOMENTE LEITURA — créditos nunca são alterados)
    # =======================================================================
    @app_commands.describe(membro="Membro para consultar (menção ou ID)")
    @admin_only
    async def cmd_usuario_ver(self, interaction: discord.Interaction, membro: discord.Member) -> None:
        if not await check_cooldown(interaction, "admin_usuario_ver"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.show_user(interaction, membro.id)

    async def show_user(self, interaction: discord.Interaction, user_id: int) -> None:
        try:
            with get_db() as con:
                ensure_user(con, user_id)
                row = con.execute(
                    """SELECT user_id, credits_cents, nickname, status, suspended_reason,
                              created_at, last_seen_at
                       FROM users WHERE user_id=?""",
                    (user_id,),
                ).fetchone()
                balance = get_balance(con, user_id)
                # Tenta resumo de sessões ativas via SessionsPricesCog; senão, SQL direto.
                sp_cog = self.bot.get_cog("SessionsPrices")
                active: list[dict[str, Any]] = []
                if sp_cog is not None and hasattr(sp_cog, "active_summary"):
                    try:
                        active = await sp_cog.active_summary(user_id) or []
                    except Exception:
                        active = []
                if not active:
                    rows = active_sessions(con, user_id)
                    active = [dict(r) for r in rows]

            user = self.bot.get_user(user_id)
            display = f"{user} (`{user_id}`)" if user else f"ID `{user_id}`"
            status_label = "✅ ativo" if row["status"] == "active" else "⛔ suspenso"
            nick = row["nickname"] or "—"
            reason = row["suspended_reason"] or "—"

            e = embed(f"👤 Usuário — {display}", "", config.COLOR_PRIMARY)
            e.add_field(name="Saldo (fictício)", value=money(balance), inline=True)
            e.add_field(name="Status", value=status_label, inline=True)
            e.add_field(name="Apelido", value=str(nick), inline=True)
            e.add_field(name="Cadastrado em", value=ts_discord(row["created_at"]), inline=True)
            e.add_field(name="Visto por último", value=ts_discord(row["last_seen_at"]), inline=True)
            if row["status"] == "suspended":
                e.add_field(name="Motivo da suspensão", value=str(reason), inline=False)

            if active:
                lines = []
                for s in active:
                    sid = s.get("id", "?")
                    video = s.get("video") or s.get("filename") or s.get("video_id", "?")
                    inicio = ts_discord(s.get("started_at"))
                    expira = ts_discord(s.get("expires_at"))
                    consumido = money(s.get("billed_cents", 0) or 0)
                    lines.append(f"• `#{sid}` {video} — início {inicio} • expira {expira} • consumido {consumido}")
                e.add_field(
                    name=f"▶️ Sessões simuladas ativas ({len(active)})",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
            else:
                e.add_field(name="▶️ Sessões ativas", value="Nenhuma.", inline=False)

            e.set_footer(text="Consulta somente leitura • créditos não são alterados por admins • " + config.FOOTER_TEXT)
            await reply(interaction, embed=e)
        except Exception as exc:
            log.exception("AdminCog: erro ao exibir usuário %s: %s", user_id, exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível consultar o usuário."))

    @admin_only
    async def cmd_usuario_listar(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "admin_usuario_listar"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.list_users(interaction)

    async def list_users(self, interaction: discord.Interaction) -> None:
        try:
            with get_db() as con:
                rows = con.execute(
                    """SELECT user_id, nickname, status, credits_cents, created_at
                       FROM users ORDER BY created_at DESC LIMIT 200"""
                ).fetchall()
            if not rows:
                await reply(interaction, embed=info_embed("Sem usuários", "Nenhum usuário cadastrado ainda."))
                return
            lines = []
            for r in rows:
                nick = r["nickname"] or "(sem apelido)"
                status = "✅" if r["status"] == "active" else "⛔"
                lines.append(
                    f"{status} `{r['user_id']}` **{nick}** — saldo {money(r['credits_cents'])} • "
                    f"cadastro {ts_discord(r['created_at'])}"
                )
            pages = chunk_lines(lines)
            pag = SimplePaginator(f"👥 Usuários cadastrados ({len(rows)} — últimos primeiro)", pages)
            msg = await interaction.followup.send(embed=pag.build_embed(), view=pag, ephemeral=True, wait=True)
            pag.message = msg
        except Exception as exc:
            log.exception("AdminCog: erro ao listar usuários: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível listar os usuários."))

    # =======================================================================
    # 2. Gestão de sessões simuladas
    # =======================================================================
    @admin_only
    async def cmd_sessoes_ativas(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "admin_sessoes_ativas"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.list_active_sessions(interaction)

    async def list_active_sessions(self, interaction: discord.Interaction) -> None:
        try:
            with get_db() as con:
                rows = con.execute(
                    """SELECT s.id, s.user_id, u.nickname, v.filename,
                              s.started_at, s.expires_at, s.billed_cents, s.duration_hours
                       FROM sessions s
                       LEFT JOIN users u ON u.user_id = s.user_id
                       LEFT JOIN videos v ON v.id = s.video_id
                       WHERE s.status='running'
                       ORDER BY s.started_at DESC"""
                ).fetchall()
            if not rows:
                await reply(interaction, embed=info_embed("Sem sessões ativas", "Nenhuma sessão simulada em andamento."))
                return
            lines = []
            for r in rows:
                nick = r["nickname"] or f"ID {r['user_id']}"
                video = r["filename"] or f"vídeo #{r['id']}"
                lines.append(
                    f"• `#{r['id']}` 👤 **{nick}** — 🎬 {video}\n"
                    f"  início {ts_discord(r['started_at'])} • expira {ts_discord(r['expires_at'])} • "
                    f"consumido {money(r['billed_cents'])}"
                )
            pages = chunk_lines(lines, max_chars=1500)
            pag = SimplePaginator(f"▶️ Sessões simuladas ativas ({len(rows)})", pages)
            msg = await interaction.followup.send(embed=pag.build_embed(), view=pag, ephemeral=True, wait=True)
            pag.message = msg
        except Exception as exc:
            log.exception("AdminCog: erro ao listar sessões: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível listar as sessões."))

    @app_commands.describe(sessao_id="Número da sessão a encerrar")
    @admin_only
    async def cmd_sessoes_encerrar(self, interaction: discord.Interaction, sessao_id: int) -> None:
        if not await check_cooldown(interaction, "admin_sessoes_encerrar"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.force_end_session(interaction, sessao_id)

    async def force_end_session(self, interaction: discord.Interaction, session_id: int) -> None:
        """Encerra uma sessão. Tenta SessionsPricesCog; senão, UPDATE direto + notificação.
        NÃO toca em créditos — o billing loop já cobrou proporcionalmente até last_billed_at."""
        try:
            # Tenta método exposto do SessionsPricesCog.
            sp_cog = self.bot.get_cog("SessionsPrices")
            if sp_cog is not None:
                for name in ("force_end_session", "admin_end_session", "encerrar_forcado", "end_session_by_id"):
                    method = getattr(sp_cog, name, None)
                    if callable(method):
                        try:
                            await method(session_id)
                            await reply(
                                interaction,
                                embed=success_embed("Sessão encerrada", f"Sessão `#{session_id}` encerrada via SessionsPricesCog."),
                            )
                            return
                        except Exception as exc:
                            log.warning("AdminCog: SessionsPricesCog.%s falhou, usando UPDATE direto: %s", name, exc)
                            break

            with get_db() as con:
                row = con.execute(
                    "SELECT id, user_id, status FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is None:
                    await reply(interaction, embed=error_embed("Não encontrada", f"Sessão `#{session_id}` não existe."))
                    return
                if row["status"] != "running":
                    await reply(
                        interaction,
                        embed=warn_embed("Já encerrada", f"Sessão `#{session_id}` está com status `{row['status']}`."),
                    )
                    return
                con.execute(
                    "UPDATE sessions SET status='ended', ended_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
                    (session_id,),
                )
                user_id = row["user_id"]

            # Notifica o dono da sessão (não altera créditos).
            await notify_user(
                self.bot,
                user_id,
                info_embed(
                    "Sessão encerrada pela administração",
                    f"Sua sessão simulada `#{session_id}` foi encerrada por um administrador. "
                    "Os créditos consumidos até o momento foram mantidos (cobrança proporcional).",
                ),
            )
            log.info("AdminCog: sessão %s encerrada por admin %s", session_id, interaction.user.id)
            await reply(
                interaction,
                embed=success_embed("Sessão encerrada", f"Sessão `#{session_id}` encerrada. Usuário notificado por DM."),
            )
        except Exception as exc:
            log.exception("AdminCog: erro ao encerrar sessão %s: %s", session_id, exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível encerrar a sessão."))

    # =======================================================================
    # 3. Gestão de vídeos
    # =======================================================================
    @admin_only
    async def cmd_videos_listar(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "admin_videos_listar"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.list_videos(interaction)

    async def list_videos(self, interaction: discord.Interaction) -> None:
        try:
            with get_db() as con:
                rows = con.execute(
                    """SELECT v.id, v.user_id, u.nickname, v.filename, v.size_bytes,
                              v.created_at, v.deleted
                       FROM videos v
                       LEFT JOIN users u ON u.user_id = v.user_id
                       ORDER BY v.created_at DESC LIMIT 300"""
                ).fetchall()
            if not rows:
                await reply(interaction, embed=info_embed("Sem vídeos", "Nenhum vídeo armazenado."))
                return
            lines = []
            for r in rows:
                nick = r["nickname"] or f"ID {r['user_id']}"
                deleted = " 🗑️" if r["deleted"] else ""
                lines.append(
                    f"• `#{r['id']}`{deleted} 🎬 **{r['filename']}** — 👤 {nick} • "
                    f"{fmt_bytes(r['size_bytes'])} • {ts_discord(r['created_at'])}"
                )
            pages = chunk_lines(lines, max_chars=1500)
            pag = SimplePaginator(f"🎬 Vídeos armazenados ({len(rows)})", pages)
            msg = await interaction.followup.send(embed=pag.build_embed(), view=pag, ephemeral=True, wait=True)
            pag.message = msg
        except Exception as exc:
            log.exception("AdminCog: erro ao listar vídeos: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível listar os vídeos."))

    @app_commands.describe(video_id="Número do vídeo a excluir")
    @admin_only
    async def cmd_videos_excluir(self, interaction: discord.Interaction, video_id: int) -> None:
        if not await check_cooldown(interaction, "admin_videos_excluir"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.delete_video(interaction, video_id)

    async def delete_video(self, interaction: discord.Interaction, video_id: int) -> None:
        """Exclui vídeo. Tenta VideosCog; senão, marca deleted + apaga arquivo do disco."""
        try:
            # Nome real do cog é "VideosCog"; tenta também "Videos" como fallback.
            v_cog = self.bot.get_cog("VideosCog") or self.bot.get_cog("Videos")
            if v_cog is not None:
                for name in ("delete_video", "excluir_video", "remove_video", "admin_delete_video"):
                    method = getattr(v_cog, name, None)
                    if callable(method):
                        try:
                            await method(video_id)
                            await reply(
                                interaction,
                                embed=success_embed("Vídeo excluído", f"Vídeo `#{video_id}` excluído via VideosCog."),
                            )
                            return
                        except Exception as exc:
                            log.warning("AdminCog: VideosCog.%s falhou, exclusão direta: %s", name, exc)
                            break

            with get_db() as con:
                row = con.execute(
                    "SELECT id, path, thumbnail_path, filename, deleted FROM videos WHERE id=?",
                    (video_id,),
                ).fetchone()
                if row is None:
                    await reply(interaction, embed=error_embed("Não encontrado", f"Vídeo `#{video_id}` não existe."))
                    return
                path = row["path"]
                thumb = row["thumbnail_path"]
                con.execute(
                    "UPDATE videos SET deleted=1, deleted_at=CURRENT_TIMESTAMP WHERE id=?",
                    (video_id,),
                )

            removed = []
            for p in (path, thumb):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed.append(os.path.basename(p))
                    except Exception as exc:
                        log.warning("AdminCog: não foi possível apagar arquivo %s: %s", p, exc)

            log.info("AdminCog: vídeo %s excluído por admin %s", video_id, interaction.user.id)
            detail = f"Arquivos removidos: {', '.join(removed)}." if removed else "Arquivo já removido ou inexistente no disco."
            await reply(
                interaction,
                embed=success_embed("Vídeo excluído", f"Vídeo `#{video_id}` marcado como excluído. {detail}"),
            )
        except Exception as exc:
            log.exception("AdminCog: erro ao excluir vídeo %s: %s", video_id, exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível excluir o vídeo."))

    # =======================================================================
    # 4. Relatório administrativo (SIMULADO) + CSV opcional
    # =======================================================================
    @app_commands.describe(periodo_dias="Período em dias (padrão 7, máximo 90)")
    @admin_only
    async def cmd_relatorio(self, interaction: discord.Interaction, periodo_dias: int = 7) -> None:
        if not await check_cooldown(interaction, "admin_relatorio"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.report(interaction, periodo_dias)

    async def _count_log_errors(self) -> int:
        """Conta erros no arquivo de log (roda em thread para não bloquear)."""
        def _read() -> int:
            log_file = config.LOG_DIR / "hapination.log"
            if not log_file.exists():
                return 0
            count = 0
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if "[ERROR]" in line or "[CRITICAL]" in line:
                            count += 1
            except Exception:
                return 0
            return count
        return await asyncio.to_thread(_read)

    async def report(self, interaction: discord.Interaction, periodo_dias: int) -> None:
        try:
            periodo_dias = max(1, min(int(periodo_dias), 90))
            cutoff = (now_utc() - timedelta(days=periodo_dias)).strftime("%Y-%m-%d %H:%M:%S")

            with get_db() as con:
                u_total = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
                u_active = con.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE status='active'"
                ).fetchone()["c"]
                u_suspended = con.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE status='suspended'"
                ).fetchone()["c"]

                sess_periodo = con.execute(
                    "SELECT COUNT(*) AS c FROM sessions WHERE started_at >= ?", (cutoff,)
                ).fetchone()["c"]
                sess_active_now = con.execute(
                    "SELECT COUNT(*) AS c FROM sessions WHERE status='running'"
                ).fetchone()["c"]

                credit_rows = con.execute(
                    """SELECT type, SUM(amount_cents) AS total, COUNT(*) AS n
                       FROM credit_logs WHERE created_at >= ? GROUP BY type""",
                    (cutoff,),
                ).fetchall()
                credit_by_type = {r["type"]: (r["total"], r["n"]) for r in credit_rows}

                vid_count = con.execute(
                    "SELECT COUNT(*) AS c FROM videos WHERE deleted=0"
                ).fetchone()["c"]
                vid_bytes = con.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) AS b FROM videos WHERE deleted=0"
                ).fetchone()["b"]

                virtual_balance = con.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) AS s FROM credit_logs"
                ).fetchone()["s"]

                csv_rows = con.execute(
                    """SELECT id, user_id, video_id, status, started_at, ended_at,
                              duration_hours, price_cents, billed_cents
                       FROM sessions WHERE started_at >= ? OR status='running'
                       ORDER BY started_at DESC""",
                    (cutoff,),
                ).fetchall()

            adicionado = credit_by_type.get("add", (0, 0))[0] + credit_by_type.get("refund", (0, 0))[0]
            consumido = abs(credit_by_type.get("consume", (0, 0))[0]) + abs(credit_by_type.get("remove", (0, 0))[0])
            correcao = credit_by_type.get("correct", (0, 0))[0]
            manual = credit_by_type.get("manual", (0, 0))[0]

            uptime = (discord.utils.utcnow() - self._start_time).total_seconds()
            errors = await self._count_log_errors()

            e = embed(
                f"📊 Relatório administrativo — últimos {periodo_dias} dias",
                "⚠️ **TODOS os valores financeiros são SIMULAÇÃO** — créditos fictícios, sem pagamentos reais.",
                config.COLOR_PRIMARY,
            )
            e.add_field(
                name="👥 Usuários cadastrados",
                value=f"Total: **{u_total}**\nAtivos: **{u_active}**\nSuspensos: **{u_suspended}**",
                inline=True,
            )
            e.add_field(
                name="▶️ Sessões simuladas",
                value=f"No período: **{sess_periodo}**\nAtivas agora: **{sess_active_now}**",
                inline=True,
            )
            e.add_field(
                name="🎬 Vídeos armazenados",
                value=f"Quantidade: **{vid_count}**\nTamanho: **{fmt_bytes(vid_bytes)}**",
                inline=True,
            )

            credit_lines = []
            type_labels = {"add": "Adicionados", "consume": "Consumidos", "remove": "Removidos",
                           "refund": "Estornados", "correct": "Corrigidos", "manual": "Manual"}
            for t, (total, n) in sorted(credit_by_type.items()):
                credit_lines.append(f"• {type_labels.get(t, t)} ({n}x): **{money(total)}**")
            e.add_field(
                name="💳 Créditos no período (SOMA por tipo)",
                value="\n".join(credit_lines) if credit_lines else "Nenhuma movimentação.",
                inline=False,
            )

            liquido = adicionado - consumido + correcao + manual
            e.add_field(
                name="💰 Movimentação financeira SIMULADA",
                value=(
                    f"Adicionado no período: **{money(adicionado)}**\n"
                    f"Consumido no período: **{money(consumido)}**\n"
                    f"Líquido no período: **{money(liquido)}**\n"
                    f"Saldo virtual total (todas as movimentações): **{money(virtual_balance)}**\n"
                    f"_Simulação — não representa dinheiro real._"
                ),
                inline=False,
            )

            e.add_field(
                name="⚙️ Desempenho",
                value=f"Uptime do bot: **{fmt_duration(uptime)}**\nErros no log: **{errors}**",
                inline=True,
            )
            e.add_field(
                name="🛠️ Manutenção",
                value=("✅ **ATIVA**" if self._is_maintenance_on() else "ℹ️ Inativa"),
                inline=True,
            )
            e.set_footer(text="Relatório simulado • " + config.FOOTER_TEXT)

            # CSV opcional com as sessões do período.
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["id", "user_id", "video_id", "status", "started_at", "ended_at",
                             "duration_hours", "price_cents", "billed_cents"])
            for r in csv_rows:
                writer.writerow([r["id"], r["user_id"], r["video_id"], r["status"],
                                 r["started_at"], r["ended_at"], r["duration_hours"],
                                 r["price_cents"], r["billed_cents"]])
            csv_bytes = buf.getvalue().encode("utf-8")
            file = discord.File(io.BytesIO(csv_bytes), filename=f"relatorio_sessoes_{periodo_dias}d.csv")

            await interaction.followup.send(embed=e, file=file, ephemeral=True)
        except Exception as exc:
            log.exception("AdminCog: erro ao gerar relatório: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível gerar o relatório."))

    def _is_maintenance_on(self) -> bool:
        try:
            with get_db() as con:
                return maintenance_on(con)
        except Exception:
            return False

    # =======================================================================
    # 5. Anúncios
    # =======================================================================
    @app_commands.describe(texto="Conteúdo do anúncio (até 2000 caracteres)")
    @admin_only
    async def cmd_anunciar(self, interaction: discord.Interaction, texto: str) -> None:
        if not await check_cooldown(interaction, "admin_anunciar"):
            return
        await self.announce(interaction, texto)

    async def announce(self, interaction: discord.Interaction, texto: str) -> None:
        try:
            texto = (texto or "").strip()
            if not texto:
                await reply(interaction, embed=error_embed("Vazio", "O texto do anúncio não pode ser vazio."))
                return
            if len(texto) > 2000:
                texto = texto[:2000]

            channel = self.find_channel(config.CHANNELS["avisos"])
            if channel is None:
                await reply(
                    interaction,
                    embed=error_embed(
                        "Canal não encontrado",
                        f"O canal {config.CHANNELS['avisos']} não existe. Peça ao PanelCog que o crie.",
                    ),
                )
                return

            e = embed("📢 Anúncio da equipe", texto, config.COLOR_ACCENT)
            e.set_author(name=f"Por {interaction.user}", icon_url=getattr(interaction.user.display_avatar, "url", None))
            sent = await channel.send(embed=e)

            with get_db() as con:
                con.execute(
                    """INSERT INTO announcements(author_id, channel_id, message_id, content)
                       VALUES (?, ?, ?, ?)""",
                    (interaction.user.id, channel.id, sent.id, texto),
                )

            log.info("AdminCog: anúncio %s enviado por %s", sent.id, interaction.user.id)
            await reply(
                interaction,
                embed=success_embed("Anúncio enviado", f"Publicado em {channel.mention} e registrado no banco."),
            )
        except Exception as exc:
            log.exception("AdminCog: erro ao enviar anúncio: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível enviar o anúncio."))

    # =======================================================================
    # 6. Manutenção (ligar: admin+owner; desligar: SÓ owner)
    # =======================================================================
    @app_commands.describe(mensagem="Mensagem opcional exibida durante a manutenção")
    @admin_only
    async def cmd_manutencao_ligar(self, interaction: discord.Interaction, mensagem: str = "") -> None:
        if not await check_cooldown(interaction, "admin_maint_ligar"):
            return
        await self.maintenance_on_action(interaction, mensagem or None)

    async def maintenance_on_action(self, interaction: discord.Interaction, message: str | None) -> None:
        try:
            message = (message or "").strip() or None
            with get_db() as con:
                set_setting(con, "maintenance_mode", "1")
                if message:
                    set_setting(con, "maintenance_message", message[:500])
                stored_msg = maintenance_message(con)

            avisos = self.find_channel(config.CHANNELS["avisos"])
            if avisos is not None:
                e = warn_embed(
                    "🛠️ Manutenção iniciada",
                    f"{stored_msg}\n\nNovas sessões simuladas e uploads estão temporariamente bloqueados. "
                    f"Sessões já ativas continuam até expirar.",
                )
                try:
                    await avisos.send(embed=e)
                except Exception as exc:
                    log.warning("AdminCog: não foi possível avisar manutenção em 📢avisos: %s", exc)

            log.info("AdminCog: manutenção ativada por %s", interaction.user.id)
            await reply(
                interaction,
                embed=success_embed("Manutenção ativada", "Novas sessões e uploads serão bloqueadas pelos demais cogs."),
            )
        except Exception as exc:
            log.exception("AdminCog: erro ao ativar manutenção: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível ativar a manutenção."))

    @owner_only
    async def cmd_manutencao_desligar(self, interaction: discord.Interaction) -> None:
        # SÓ proprietário — owner_only aplicado via check manual (registro manual).
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Acesso negado", "Desligar a manutenção é exclusivo do proprietário."),
                ephemeral=True,
            )
            return
        if not await check_cooldown(interaction, "admin_maint_desligar"):
            return
        await interaction.response.defer(ephemeral=True)
        await self.maintenance_off(interaction)

    async def maintenance_off(self, interaction: discord.Interaction) -> None:
        try:
            with get_db() as con:
                set_setting(con, "maintenance_mode", "0")

            avisos = self.find_channel(config.CHANNELS["avisos"])
            if avisos is not None:
                e = success_embed("✅ Manutenção concluída", "Novas sessões simuladas e uploads voltaram ao normal.")
                try:
                    await avisos.send(embed=e)
                except Exception as exc:
                    log.warning("AdminCog: não foi possível avisar fim da manutenção: %s", exc)

            log.info("AdminCog: manutenção desativada por %s (owner)", interaction.user.id)
            await reply(interaction, embed=success_embed("Manutenção desativada", "O bot voltou a operar normalmente."))
        except Exception as exc:
            log.exception("AdminCog: erro ao desativar manutenção: %s", exc)
            await reply(interaction, embed=error_embed("Erro", "Não foi possível desativar a manutenção."))


class _InputError(Exception):
    """Erro de validação de entrada em modais."""


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
