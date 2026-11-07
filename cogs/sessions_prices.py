"""Cog de sessões simuladas e preços — Hapination Live.

Responsabilidades (ver SPEC.md seção 2.2):
  - CRUD de durações/preços (tabela `durations`, até 48h) via /admin duracao ...
  - Iniciar / renovar / encerrar sessões simuladas com Views de confirmação.
  - Loop tasks.loop(minutes=1): heartbeat, cobrança proporcional minuto a minuto
    (sem débito antecipado, teto em price_cents, UPDATE atômico), avisos de
    expiração e saldo baixo por DM, encerramento automático.
  - Recuperação após downtime (on_ready): estende expires_at, não cobra o gap,
    notifica usuários.
  - Métodos expostos: start_flow, renew_flow, end_flow, active_summary,
    history_summary.

NUNCA afirmar transmissão real; sempre dizer "sessão simulada". Créditos fictícios.
NÃO modifique core/, bot.py, SPEC.md nem outros cogs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.common import (
    admin_only,
    check_cooldown,
    embed,
    error_embed,
    fmt_duration,
    info_embed,
    is_suspended,
    log,
    money,
    not_suspended,
    notify_user,
    success_embed,
    warn_embed,
)
from core.database import ensure_user, get_db, get_setting, set_setting
from core.services import (
    active_sessions,
    active_sessions_count,
    adjust_credits,
    billing_step,
    get_balance,
    list_durations,
    maintenance_message,
    maintenance_on,
    max_sessions_per_user,
    now_utc,
    parse_iso,
    price_for_hours,
    remaining_credit_minutes,
)


# ---------------------------------------------------------------------------
# Helpers locais
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Formato esperado pelo SQLite (CURRENT_TIMESTAMP): 'YYYY-MM-DD HH:MM:SS' UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return discord.utils.format_dt(dt, style="f")


def _video_label(row) -> str:
    name = row["filename"] if "filename" in row.keys() else f"vídeo #{row['id']}"
    return (name[:77] + "...") if len(name) > 80 else name


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class StartFlowView(discord.ui.View):
    """Seleção de vídeo + duração, com resumo de confirmação embutido."""

    def __init__(self, cog: "SessionsPricesCog", user_id: int, videos, durations):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.videos = {v["id"]: v for v in videos}
        self.durations = {d["hours"]: d for d in durations}
        self.selected_video: Optional[int] = None
        self.selected_hours: Optional[int] = None

        vid_opts = [
            discord.SelectOption(label=_video_label(v), value=str(v["id"]))
            for v in videos[:25]
        ]
        self.video_sel = discord.ui.Select(
            placeholder="1. Escolha o vídeo da sua biblioteca...",
            options=vid_opts,
            row=0,
        )
        self.video_sel.callback = self._on_video
        self.add_item(self.video_sel)

        dur_opts = [
            discord.SelectOption(
                label=f"{d['hours']}h — {money(d['price_cents'])}",
                value=str(d["hours"]),
            )
            for d in durations
        ]
        self.dur_sel = discord.ui.Select(
            placeholder="2. Escolha a duração da sessão simulada...",
            options=dur_opts,
            row=1,
        )
        self.dur_sel.callback = self._on_duration
        self.add_item(self.dur_sel)

    def _summary_embed(self) -> discord.Embed:
        e = info_embed(
            "Iniciar sessão simulada",
            "Selecione o vídeo e a duração. O valor é **provisório** e só é cobrado "
            "minuto a minuto enquanto a sessão estiver aberta. Créditos fictícios.",
        )
        if self.selected_video is not None:
            v = self.videos.get(self.selected_video)
            if v:
                e.add_field(name="Vídeo", value=_video_label(v), inline=False)
        if self.selected_hours is not None:
            d = self.durations.get(self.selected_hours)
            if d:
                with get_db() as con:
                    balance = get_balance(con, self.user_id)
                e.add_field(name="Duração", value=f"{d['hours']}h", inline=True)
                e.add_field(name="Preço total (teto)", value=money(d["price_cents"]), inline=True)
                e.add_field(
                    name="Saldo atual",
                    value=f"{money(balance)}",
                    inline=True,
                )
                restante = max(0, balance - d["price_cents"])
                e.add_field(
                    name="Saldo projetado após a sessão",
                    value=money(restante),
                    inline=False,
                )
        return e

    async def _refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(embed=self._summary_embed(), view=self)
        except discord.NotFound:
            pass

    async def _on_video(self, interaction: discord.Interaction) -> None:
        self.selected_video = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_duration(self, interaction: discord.Interaction) -> None:
        self.selected_hours = int(interaction.data["values"][0])
        await self._refresh(interaction)

    @discord.ui.button(label="Confirmar início", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_video is None or self.selected_hours is None:
            await interaction.response.send_message(
                embed=warn_embed("Seleção incompleta", "Escolha um vídeo e uma duração antes de confirmar."),
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Iniciando sessão simulada...", "Validando saldo e reservando tempo."),
            view=None,
        )
        await self.cog._do_start(interaction, self.user_id, self.selected_video, self.selected_hours)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Cancelado", "Nenhuma sessão simulada foi iniciada."),
            view=None,
        )


class RenewFlowView(discord.ui.View):
    """Seleciona sessão ativa (ou última encerrada como fallback) + duração extra."""

    def __init__(self, cog: "SessionsPricesCog", user_id: int, sessions, durations, fallback: bool):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.sessions = {s["id"]: s for s in sessions}
        self.durations = {d["hours"]: d for d in durations}
        self.fallback = fallback
        self.selected_session: Optional[int] = None
        self.selected_hours: Optional[int] = None

        sess_opts = []
        for s in sessions[:25]:
            exp = parse_iso(s["expires_at"])
            label = f"#{s['id']} — vídeo {s['video_id']}"
            desc = f"expira {_fmt_dt(exp)}" if not fallback else "última sessão encerrada"
            sess_opts.append(discord.SelectOption(label=label[:90], value=str(s["id"]), description=desc[:50]))
        self.sess_sel = discord.ui.Select(
            placeholder="Escolha a sessão para renovar...", options=sess_opts, row=0
        )
        self.sess_sel.callback = self._on_session
        self.add_item(self.sess_sel)

        dur_opts = [
            discord.SelectOption(label=f"+{d['hours']}h — {money(d['price_cents'])}", value=str(d["hours"]))
            for d in durations
        ]
        self.dur_sel = discord.ui.Select(
            placeholder="Duração adicional...", options=dur_opts, row=1
        )
        self.dur_sel.callback = self._on_duration
        self.add_item(self.dur_sel)

    def _summary_embed(self) -> discord.Embed:
        title = "Renovar sessão simulada"
        desc = ("Não havia sessão ativa. Será iniciada uma **nova** sessão simulada "
                "usando o vídeo da última sessão registrada.") if self.fallback else \
               "Estenda uma sessão simulada **ainda ativa**. O tempo extra é cobrado proporcionalmente."
        e = info_embed(title, desc)
        if self.selected_session is not None and self.selected_hours is not None:
            d = self.durations[self.selected_hours]
            with get_db() as con:
                balance = get_balance(con, self.user_id)
            e.add_field(name="Duração adicional", value=f"+{d['hours']}h", inline=True)
            e.add_field(name="Acréscimo no teto de preço", value=money(d["price_cents"]), inline=True)
            e.add_field(name="Saldo atual", value=money(balance), inline=True)
        return e

    async def _refresh(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(embed=self._summary_embed(), view=self)
        except discord.NotFound:
            pass

    async def _on_session(self, interaction: discord.Interaction) -> None:
        self.selected_session = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_duration(self, interaction: discord.Interaction) -> None:
        self.selected_hours = int(interaction.data["values"][0])
        await self._refresh(interaction)

    @discord.ui.button(label="Confirmar renovação", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_session is None or self.selected_hours is None:
            await interaction.response.send_message(
                embed=warn_embed("Seleção incompleta", "Escolha a sessão e a duração adicional."),
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Processando renovação...", "Aplicando tempo extra à sessão simulada."),
            view=None,
        )
        await self.cog._do_renew(interaction, self.user_id, self.selected_session, self.selected_hours, self.fallback)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Cancelado", "Nenhuma renovação foi realizada."), view=None
        )


class EndFlowView(discord.ui.View):
    """Seleciona qual sessão ativa encerrar manualmente."""

    def __init__(self, cog: "SessionsPricesCog", user_id: int, sessions):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.sessions = {s["id"]: s for s in sessions}
        opts = []
        for s in sessions[:25]:
            exp = parse_iso(s["expires_at"])
            opts.append(discord.SelectOption(
                label=f"#{s['id']} — vídeo {s['video_id']}",
                value=str(s["id"]),
                description=f"expira {_fmt_dt(exp)}"[:50],
            ))
        self.sess_sel = discord.ui.Select(placeholder="Escolha a sessão para encerrar...", options=opts)
        self.sess_sel.callback = self._on_session
        self.add_item(self.sess_sel)
        self.selected: Optional[int] = None

    async def _on_session(self, interaction: discord.Interaction) -> None:
        self.selected = int(interaction.data["values"][0])
        s = self.sessions[self.selected]
        exp = parse_iso(s["expires_at"])
        e = warn_embed(
            "Encerrar sessão simulada",
            f"Sessão **#{s['id']}** (expira em {_fmt_dt(exp)}).\n"
            f"Consumido até agora: **{money(s['billed_cents'])}**.\n"
            "Se o tempo usado for ≤ 2 minutos, o valor cobrado é estornado automaticamente.",
        )
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Encerrar agora", style=discord.ButtonStyle.danger)
    async def end_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected is None:
            await interaction.response.send_message(
                embed=warn_embed("Selecione uma sessão", "Escolha uma sessão na lista antes de encerrar."),
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Encerrando sessão simulada...", "Gerando resumo e aplicando tolerância de 2 minutos."),
            view=None,
        )
        await self.cog._do_end(interaction, self.user_id, self.selected)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Cancelado", "Nenhuma sessão foi encerrada."), view=None
        )


# ---------------------------------------------------------------------------
# Cog principal
# ---------------------------------------------------------------------------

class SessionsPricesCog(commands.Cog, name="SessionsPrices"):
    """Gestão de durações/preços e ciclo de vida das sessões simuladas."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ready = False
        self._duracao_group: Optional[app_commands.Group] = None

    # -- propriedades ------------------------------------------------------
    @property
    def _actor_id(self) -> int:
        """ID usado como ator em logs de crédito gerados pelo sistema (cobrança/refund)."""
        return self.bot.user.id if self.bot.user else 0

    # -- ciclo de vida do cog ----------------------------------------------
    async def cog_load(self) -> None:
        """Garante o subgrupo /admin duracao (compartilhado com outros cogs)."""
        tree = self.bot.tree
        admin_cmd = tree.get_command("admin")
        if admin_cmd is None:
            admin_cmd = app_commands.Group(
                name="admin", description="Comandos administrativos do Hapination Live"
            )
            tree.add_command(admin_cmd)
        if isinstance(admin_cmd, app_commands.Group) and admin_cmd.get_command("duracao") is None:
            g = app_commands.Group(
                name="duracao", description="Gerir durações e preços de sessões simuladas"
            )
            g.command(name="criar", description="Criar nova duração e preço (até 48h)")(self._duracao_criar)
            g.command(name="alterar", description="Alterar o preço de uma duração existente")(self._duracao_alterar)
            g.command(name="excluir", description="Desativar uma duração (não apaga histórico)")(self._duracao_excluir)
            g.command(name="listar", description="Listar todas as durações e preços")(self._duracao_listar)
            admin_cmd.add_command(g)
            self._duracao_group = g
            log.info("Subgrupo /admin duracao registrado por SessionsPricesCog.")

    async def cog_unload(self) -> None:
        if self.billing_loop.is_running():
            self.billing_loop.cancel()

    # -- on_ready: recuperação de downtime + arranque do loop ---------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._ready:
            return
        self._ready = True
        try:
            await self._recover_downtime()
        except Exception:
            log.exception("Falha na recuperação de downtime.")
        if not self.billing_loop.is_running():
            try:
                self.billing_loop.start()
                log.info("Loop de cobrança/heartbeat iniciado.")
            except Exception:
                log.exception("Não foi possível iniciar o loop de cobrança.")

    async def _recover_downtime(self) -> None:
        """Lê last_heartbeat, estende expires_at das sessões running pelo gap
        (tempo de indisponibilidade NÃO é cobrado), zera avisos se ainda longe
        e notifica os usuários por DM."""
        with get_db() as con:
            raw = get_setting(con, "last_heartbeat", None)
            last_hb = parse_iso(raw) if raw else None
            now = now_utc()
            if last_hb is None:
                gap_seconds = 0
            else:
                gap_seconds = int((now - last_hb).total_seconds())
            # Gap menor que ~90s é normal (reinício rápido). Não notifica.
            if gap_seconds < 90:
                set_setting(con, "last_heartbeat", _iso(now))
                return
            rows = con.execute(
                "SELECT * FROM sessions WHERE status='running'"
            ).fetchall()
            notified: dict[int, int] = {}  # user_id -> total segundos estendidos
            for r in rows:
                exp = parse_iso(r["expires_at"]) or now
                new_exp = exp + timedelta(seconds=gap_seconds)
                new_dt = r["downtime_seconds"] + gap_seconds
                # Não cobra o gap: avança last_billed_at para agora.
                new_warned_expiry = r["warned_expiry"]
                new_warned_low = r["warned_low_credit"]
                if (new_exp - now).total_seconds() > config.EXPIRY_WARN_MINUTES * 60:
                    new_warned_expiry = 0
                    new_warned_low = 0
                con.execute(
                    """UPDATE sessions
                       SET expires_at=?, downtime_seconds=?, last_billed_at=?,
                           warned_expiry=?, warned_low_credit=?
                       WHERE id=?""",
                    (_iso(new_exp), new_dt, _iso(now), new_warned_expiry, new_warned_low, r["id"]),
                )
                notified[r["user_id"]] = notified.get(r["user_id"], 0) + gap_seconds
            set_setting(con, "last_heartbeat", _iso(now))

        for user_id, secs in notified.items():
            e = info_embed(
                "Bot voltou ao ar",
                f"O Hapination Live ficou indisponível por **{fmt_duration(secs)}**.\n"
                "Esse tempo **não foi cobrado** e foi adicionado de volta às suas "
                "sessões simuladas ativas.",
            )
            await notify_user(self.bot, user_id, e)
        if notified:
            log.info("Downtime de %ds recuperado: %d usuário(s) notificado(s).", gap_seconds, len(notified))

    # =======================================================================
    # 1) CRUD de durações / preços  (/admin duracao ...)
    # =======================================================================

    @admin_only
    async def _duracao_criar(
        self, interaction: discord.Interaction, horas: int, preco_em_centavos: int
    ) -> None:
        if not await check_cooldown(interaction, "admin_duracao"):
            return
        if horas <= 0 or horas > config.MAX_DURATION_HOURS:
            await interaction.response.send_message(
                embed=error_embed("Duração inválida", f"A duração deve ser entre 1h e {config.MAX_DURATION_HOURS}h."),
                ephemeral=True,
            )
            return
        if preco_em_centavos < 0:
            await interaction.response.send_message(
                embed=error_embed("Preço inválido", "O preço não pode ser negativo."),
                ephemeral=True,
            )
            return
        with get_db() as con:
            existing = con.execute(
                "SELECT * FROM durations WHERE hours=?", (horas,)
            ).fetchone()
            if existing:
                # Se existe mas inativa, reativa e atualiza preço.
                con.execute(
                    "UPDATE durations SET price_cents=?, active=1 WHERE hours=?",
                    (preco_em_centavos, horas),
                )
                msg = f"Duração de **{horas}h** já existia e foi reativada/atualizada."
            else:
                con.execute(
                    "INSERT INTO durations(hours, price_cents, active) VALUES (?, ?, 1)",
                    (horas, preco_em_centavos),
                )
                msg = f"Duração de **{horas}h** criada com sucesso."
        await interaction.response.send_message(
            embed=success_embed("Duração registrada", f"{msg}\nPreço: **{money(preco_em_centavos)}**."),
            ephemeral=True,
        )

    @admin_only
    async def _duracao_alterar(
        self, interaction: discord.Interaction, horas: int, preco_em_centavos: int
    ) -> None:
        if not await check_cooldown(interaction, "admin_duracao"):
            return
        if preco_em_centavos < 0:
            await interaction.response.send_message(
                embed=error_embed("Preço inválido", "O preço não pode ser negativo."),
                ephemeral=True,
            )
            return
        with get_db() as con:
            cur = con.execute(
                "UPDATE durations SET price_cents=? WHERE hours=?",
                (preco_em_centavos, horas),
            )
            if cur.rowcount == 0:
                await interaction.response.send_message(
                    embed=error_embed("Não encontrada", f"Não existe duração de {horas}h cadastrada."),
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(
            embed=success_embed(
                "Preço atualizado",
                f"Duração de **{horas}h** agora custa **{money(preco_em_centavos)}**.",
            ),
            ephemeral=True,
        )

    @admin_only
    async def _duracao_excluir(self, interaction: discord.Interaction, horas: int) -> None:
        if not await check_cooldown(interaction, "admin_duracao"):
            return
        with get_db() as con:
            # Desativa em vez de apagar, para preservar histórico de sessões.
            cur = con.execute("UPDATE durations SET active=0 WHERE hours=?", (horas,))
            if cur.rowcount == 0:
                await interaction.response.send_message(
                    embed=error_embed("Não encontrada", f"Não existe duração de {horas}h cadastrada."),
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(
            embed=success_embed(
                "Duração desativada",
                f"A duração de **{horas}h** foi desativada e não aparecerá mais para novos inícios. "
                "Sessões já em andamento não são afetadas.",
            ),
            ephemeral=True,
        )

    @admin_only
    async def _duracao_listar(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "admin_duracao"):
            return
        with get_db() as con:
            rows = list_durations(con, only_active=False)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Sem durações", "Nenhuma duração cadastrada ainda."),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            status = "✅ ativa" if r["active"] else "🚫 inativa"
            lines.append(f"• **{r['hours']}h** — {money(r['price_cents'])}  ({status})")
        e = info_embed("Durações e preços", "\n".join(lines))
        await interaction.response.send_message(embed=e, ephemeral=True)

    # =======================================================================
    # 2) Iniciar sessão  (/iniciar + start_flow)
    # =======================================================================

    @app_commands.command(name="iniciar", description="Iniciar uma sessão simulada (vídeo + duração)")
    @not_suspended
    async def cmd_iniciar(self, interaction: discord.Interaction) -> None:
        await self.start_flow(interaction)

    async def start_flow(self, interaction: discord.Interaction) -> None:
        """Fluxo de início, chamado por /iniciar e pelo botão do painel."""
        if not await check_cooldown(interaction, "start_session"):
            return
        user_id = interaction.user.id
        with get_db() as con:
            ensure_user(con, user_id)
            if maintenance_on(con):
                await self._respond(
                    interaction,
                    error_embed("Manutenção em andamento", maintenance_message(con)),
                )
                return
            if active_sessions_count(con, user_id) >= max_sessions_per_user(con):
                await self._respond(
                    interaction,
                    error_embed(
                        "Limite de sessões atingido",
                        f"Você já tem o máximo de **{max_sessions_per_user(con)}** sessão(ões) simulada(s) "
                        "ativa(s). Encerre uma antes de abrir outra.",
                    ),
                )
                return
            videos = con.execute(
                """SELECT id, filename FROM videos
                   WHERE user_id=? AND deleted=0
                   ORDER BY created_at DESC LIMIT 25""",
                (user_id,),
            ).fetchall()
            durations = list_durations(con, only_active=True)
        if not videos:
            await self._respond(
                interaction,
                error_embed(
                    "Sem vídeos",
                    "Você ainda não tem vídeos na biblioteca. Envie um vídeo primeiro "
                    "(use 📤 Enviar vídeo no seu painel ou /enviar_video).",
                ),
            )
            return
        if not durations:
            await self._respond(
                interaction,
                error_embed("Sem durações disponíveis", "Nenhuma duração/preço está ativa no momento. Procure a administração."),
            )
            return
        view = StartFlowView(self, user_id, videos, durations)
        await self._respond(interaction, view._summary_embed(), view=view)

    async def _do_start(
        self, interaction: discord.Interaction, user_id: int, video_id: int, hours: int
    ) -> None:
        """Confirma e cria a sessão. Chamado pela View de confirmação."""
        with get_db() as con:
            ensure_user(con, user_id)
            if maintenance_on(con):
                await self._respond(interaction, error_embed("Manutenção", maintenance_message(con)))
                return
            if active_sessions_count(con, user_id) >= max_sessions_per_user(con):
                await self._respond(
                    interaction,
                    error_embed("Limite de sessões atingido", "Outra sessão foi aberta enquanto você escolhia."),
                )
                return
            video = con.execute(
                "SELECT id, filename FROM videos WHERE id=? AND user_id=? AND deleted=0",
                (video_id, user_id),
            ).fetchone()
            if not video:
                await self._respond(interaction, error_embed("Vídeo indisponível", "O vídeo selecionado não existe mais."))
                return
            price = price_for_hours(con, hours)
            if price is None:
                await self._respond(interaction, error_embed("Duração indisponível", "A duração selecionada não está mais ativa."))
                return
            balance = get_balance(con, user_id)
            if balance < price:
                await self._respond(
                    interaction,
                    error_embed(
                        "Saldo insuficiente",
                        f"Esta sessão custa até **{money(price)}** e seu saldo é **{money(balance)}**.\n"
                        "A cobrança é minuto a minuto, mas é preciso ter saldo para o preço total.",
                    ),
                )
                return
            now = now_utc()
            expires = now + timedelta(hours=hours)
            cur = con.execute(
                """INSERT INTO sessions(
                       user_id, video_id, status, started_at, expires_at,
                       duration_hours, price_cents, billed_cents, billed_millicents,
                       last_billed_at, warned_expiry, warned_low_credit,
                       downtime_seconds, renewals
                   ) VALUES (?, ?, 'running', ?, ?, ?, ?, 0, 0, ?, 0, 0, 0, 0)""",
                (user_id, video_id, _iso(now), _iso(expires), hours, price, _iso(now)),
            )
            session_id = cur.lastrowid
        e = success_embed(
            "Sessão simulada iniciada",
            f"**#{session_id}** — vídeo `{_video_label(video)}`\n"
            f"Duração: **{hours}h** (expira em {_fmt_dt(expires)})\n"
            f"Preço total (teto): **{money(price)}** — cobrado minuto a minuto.\n"
            f"Saldo: **{money(balance)}**.",
        )
        await self._respond(interaction, e)
        log.info("Sessão #%s iniciada por usuário %s (vídeo %s, %sh, teto %sc).",
                 session_id, user_id, video_id, hours, price)

    # =======================================================================
    # 3) Renovar sessão ATIVA  (/renovar + renew_flow)
    # =======================================================================

    @app_commands.command(name="renovar", description="Renovar/estender uma sessão simulada ativa")
    @not_suspended
    async def cmd_renovar(self, interaction: discord.Interaction) -> None:
        await self.renew_flow(interaction)

    async def renew_flow(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "renew_session"):
            return
        user_id = interaction.user.id
        with get_db() as con:
            ensure_user(con, user_id)
            if maintenance_on(con):
                await self._respond(interaction, error_embed("Manutenção", maintenance_message(con)))
                return
            actives = active_sessions(con, user_id)
            fallback = False
            sessions = actives
            if not actives:
                # Fallback: última sessão encerrada/expirada.
                last = con.execute(
                    """SELECT * FROM sessions
                       WHERE user_id=? AND status IN ('ended', 'expired')
                       ORDER BY ended_at DESC, started_at DESC LIMIT 1""",
                    (user_id,),
                ).fetchall()
                if not last:
                    await self._respond(
                        interaction,
                        error_embed(
                            "Nada para renovar",
                            "Você não tem sessão ativa nem histórico de sessões. Use /iniciar.",
                        ),
                    )
                    return
                sessions = last
                fallback = True
            durations = list_durations(con, only_active=True)
        if not durations:
            await self._respond(
                interaction,
                error_embed("Sem durações", "Nenhuma duração está ativa no momento."),
            )
            return
        view = RenewFlowView(self, user_id, sessions, durations, fallback)
        await self._respond(interaction, view._summary_embed(), view=view)

    async def _do_renew(
        self,
        interaction: discord.Interaction,
        user_id: int,
        session_id: int,
        extra_hours: int,
        fallback: bool,
    ) -> None:
        with get_db() as con:
            ensure_user(con, user_id)
            extra_price = price_for_hours(con, extra_hours)
            if extra_price is None:
                await self._respond(interaction, error_embed("Duração indisponível", "A duração escolhida não está mais ativa."))
                return
            balance = get_balance(con, user_id)
            if balance < extra_price:
                await self._respond(
                    interaction,
                    error_embed(
                        "Saldo insuficiente",
                        f"A renovação custa até **{money(extra_price)}** e seu saldo é **{money(balance)}**.",
                    ),
                )
                return
            row = con.execute(
                "SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)
            ).fetchone()
            if not row:
                await self._respond(interaction, error_embed("Sessão não encontrada", "A sessão selecionada não existe."))
                return

            if fallback or row["status"] != "running":
                # Fallback: inicia nova sessão com o vídeo da última sessão.
                video = con.execute(
                    "SELECT id, filename FROM videos WHERE id=? AND user_id=? AND deleted=0",
                    (row["video_id"], user_id),
                ).fetchone()
                if not video:
                    await self._respond(
                        interaction,
                        error_embed("Vídeo indisponível", "O vídeo da última sessão foi excluído. Use /iniciar com outro vídeo."),
                    )
                    return
                if active_sessions_count(con, user_id) >= max_sessions_per_user(con):
                    await self._respond(
                        interaction,
                        error_embed("Limite de sessões atingido", "Você já tem sessões ativas demais."),
                    )
                    return
                now = now_utc()
                expires = now + timedelta(hours=extra_hours)
                cur = con.execute(
                    """INSERT INTO sessions(
                           user_id, video_id, status, started_at, expires_at,
                           duration_hours, price_cents, billed_cents, billed_millicents,
                           last_billed_at, warned_expiry, warned_low_credit,
                           downtime_seconds, renewals
                       ) VALUES (?, ?, 'running', ?, ?, ?, ?, 0, 0, ?, 0, 0, 0, 0)""",
                    (user_id, row["video_id"], _iso(now), _iso(expires), extra_hours, extra_price, _iso(now)),
                )
                new_id = cur.lastrowid
                await self._respond(
                    interaction,
                    success_embed(
                        "Nova sessão simulada iniciada (renovação)",
                        f"Não havia sessão ativa. Sessão **#{new_id}** criada a partir da última "
                        f"(vídeo `{_video_label(video)}`, {extra_hours}h, expira em {_fmt_dt(expires)}).",
                    ),
                )
                log.info("Renovação-fallback: nova sessão #%s para usuário %s.", new_id, user_id)
                return

            # Caso normal: estende sessão ATIVA. Aumenta teto de preço e prazo;
            # cobrança continua proporcional minuto a minuto (sem débito antecipado).
            now = now_utc()
            exp = parse_iso(row["expires_at"]) or now
            new_exp = exp + timedelta(hours=extra_hours)
            new_price = row["price_cents"] + extra_price
            new_hours = row["duration_hours"] + extra_hours
            con.execute(
                """UPDATE sessions
                   SET expires_at=?, price_cents=?, duration_hours=?, renewals=renewals+1,
                       warned_expiry=0, warned_low_credit=0
                   WHERE id=? AND status='running'""",
                (_iso(new_exp), new_price, new_hours, session_id),
            )
        await self._respond(
            interaction,
            success_embed(
                "Sessão simulada renovada",
                f"Sessão **#{session_id}** estendida em **+{extra_hours}h**.\n"
                f"Nova expiração: {_fmt_dt(new_exp)}\n"
                f"Novo teto de preço: **{money(new_price)}** (acréscimo de {money(extra_price)}, "
                "cobrado minuto a minuto).",
            ),
        )
        log.info("Sessão #%s renovada +%sh por usuário %s.", session_id, extra_hours, user_id)

    # =======================================================================
    # 4) Encerrar manual  (/encerrar + end_flow)
    # =======================================================================

    @app_commands.command(name="encerrar", description="Encerrar uma sessão simulada ativa")
    @not_suspended
    async def cmd_encerrar(self, interaction: discord.Interaction) -> None:
        await self.end_flow(interaction)

    async def end_flow(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "end_session"):
            return
        user_id = interaction.user.id
        with get_db() as con:
            ensure_user(con, user_id)
            actives = active_sessions(con, user_id)
        if not actives:
            await self._respond(
                interaction,
                info_embed("Sem sessões ativas", "Você não tem nenhuma sessão simulada em andamento."),
            )
            return
        view = EndFlowView(self, user_id, actives)
        e = info_embed(
            "Encerrar sessão simulada",
            "Escolha qual sessão ativa deseja encerrar. O resumo é enviado por DM.",
        )
        await self._respond(interaction, e, view=view)

    async def _do_end(self, interaction: discord.Interaction, user_id: int, session_id: int) -> None:
        ok = await self._finish_session(session_id, final_status="ended", reason="encerrada manualmente")
        if not ok:
            await self._respond(
                interaction,
                error_embed("Não foi possível encerrar", "A sessão não existe ou já foi encerrada."),
            )
            return
        await self._respond(
            interaction,
            success_embed(
                "Sessão simulada encerrada",
                f"Sessão **#{session_id}** encerrada. O resumo completo foi enviado por DM.",
            ),
        )

    # =======================================================================
    # 5) Loop de cobrança / heartbeat / avisos / encerramento automático
    # =======================================================================

    @tasks.loop(minutes=1)
    async def billing_loop(self) -> None:
        try:
            await self._tick()
        except Exception:
            log.exception("Erro no tick do loop de sessões.")

    async def _tick(self) -> None:
        now = now_utc()
        with get_db() as con:
            set_setting(con, "last_heartbeat", _iso(now))
            rows = con.execute("SELECT * FROM sessions WHERE status='running'").fetchall()

        for r in rows:
            try:
                await self._process_session(r, now)
            except Exception:
                log.exception("Erro ao processar sessão #%s no loop.", r["id"])

    async def _process_session(self, r, now: datetime) -> None:
        sid = r["id"]
        user_id = r["user_id"]
        total_minutes = max(1, r["duration_hours"] * 60)
        price_cents = r["price_cents"]

        last_billed = parse_iso(r["last_billed_at"]) or parse_iso(r["started_at"]) or now
        elapsed_min = int((now - last_billed).total_seconds() // 60)
        # Segurança: nunca cobra mais que 2 min num único tick (recovery já tratou downtime).
        elapsed_min = max(0, min(elapsed_min, 2))

        chargeable = 0
        if elapsed_min >= 1:
            new_milli, new_cents, chargeable = billing_step(
                price_cents, total_minutes, r["billed_millicents"], r["billed_cents"], elapsed_min
            )
            with get_db() as con:
                # UPDATE atômico: se outra instância já cobrou (last_billed_at mudou), rowcount=0.
                cur = con.execute(
                    """UPDATE sessions
                       SET billed_millicents=?, billed_cents=?, last_billed_at=?
                       WHERE id=? AND last_billed_at=?""",
                    (new_milli, new_cents, _iso(now), sid, r["last_billed_at"]),
                )
                if cur.rowcount == 0:
                    return  # cobrança duplicada evitada
                if chargeable > 0:
                    balance = get_balance(con, user_id)
                    to_charge = min(chargeable, max(0, balance))
                    if to_charge > 0:
                        adjust_credits(
                            con, user_id, -to_charge,
                            actor_id=self._actor_id, log_type="consume",
                            reason=f"Cobrança minuto a minuto sessão #{sid}",
                            session_id=sid,
                        )
                    balance = get_balance(con, user_id)
                else:
                    balance = get_balance(con, user_id)

            # Saldo esgotou? Encerra.
            if chargeable > 0 and balance <= 0:
                await self._finish_session(sid, final_status="ended", reason="saldo esgotado")
                return

        # Releio estado atualizado para avisos/expiração.
        with get_db() as con:
            fresh = con.execute("SELECT * FROM sessions WHERE id=? AND status='running'", (sid,)).fetchone()
            if fresh is None:
                return
            balance = get_balance(con, user_id)
            exp = parse_iso(fresh["expires_at"]) or now

            # (c) Aviso 5 min antes de expirar.
            secs_to_expire = (exp - now).total_seconds()
            if not fresh["warned_expiry"] and 0 < secs_to_expire <= config.EXPIRY_WARN_MINUTES * 60:
                con.execute("UPDATE sessions SET warned_expiry=1 WHERE id=?", (sid,))
                await notify_user(
                    self.bot, user_id,
                    warn_embed(
                        "Sessão simulada perto de expirar",
                        f"A sessão **#{sid}** expira em menos de **{config.EXPIRY_WARN_MINUTES} minutos** "
                        f"(às {_fmt_dt(exp)}). Use /renovar se quiser estender.",
                    ),
                )

            # (d) Aviso de saldo <= 5 min de crédito.
            if not fresh["warned_low_credit"]:
                rem_min = remaining_credit_minutes(balance, fresh["price_cents"], total_minutes)
                if rem_min <= config.LOW_CREDIT_WARN_MINUTES:
                    con.execute("UPDATE sessions SET warned_low_credit=1 WHERE id=?", (sid,))
                    await notify_user(
                        self.bot, user_id,
                        warn_embed(
                            "Saldo baixo",
                            f"Seu saldo ({money(balance)}) cobre menos de "
                            f"**{config.LOW_CREDIT_WARN_MINUTES} minutos** da sessão **#{sid}**. "
                            "A sessão será encerrada automaticamente quando o saldo acabar.",
                        ),
                    )

            # (e) Expirou? Encerra automaticamente.
            if now >= exp:
                await self._finish_session(sid, final_status="expired", reason="expirou")

    # -- finalização de sessão (manual / expirada / saldo) ------------------
    async def _finish_session(self, session_id: int, final_status: str, reason: str) -> bool:
        """Encerra uma sessão, aplica tolerância de 2min (refund se ≤120s),
        registra histórico e envia resumo por DM. Retorna False se não existia."""
        now = now_utc()
        with get_db() as con:
            row = con.execute(
                "SELECT * FROM sessions WHERE id=? AND status='running'", (session_id,)
            ).fetchone()
            if row is None:
                return False
            user_id = row["user_id"]
            started = parse_iso(row["started_at"]) or now
            downtime = row["downtime_seconds"] or 0
            used_seconds = max(0, (now - started).total_seconds() - downtime)
            billed = row["billed_cents"]

            # Tolerância de 2 minutos: estorna o que foi cobrado.
            refunded = 0
            if used_seconds <= config.GRACE_SECONDS and billed > 0:
                adjust_credits(
                    con, user_id, billed,
                    actor_id=self._actor_id, log_type="refund",
                    reason=f"Estorno tolerância 2min sessão #{session_id}",
                    session_id=session_id,
                )
                refunded = billed
                billed = 0
                con.execute("UPDATE sessions SET billed_cents=0 WHERE id=?", (session_id,))

            con.execute(
                """UPDATE sessions
                   SET status=?, ended_at=?
                   WHERE id=? AND status='running'""",
                (final_status, _iso(now), session_id),
            )
            video = con.execute(
                "SELECT filename FROM videos WHERE id=?", (row["video_id"],)
            ).fetchone()
            balance = get_balance(con, user_id)

        video_name = video["filename"] if video else f"vídeo #{row['video_id']}"
        e = info_embed(
            f"Sessão simulada #{session_id} encerrada",
            f"Motivo: {reason}.",
        )
        e.add_field(name="Vídeo", value=video_name[:100], inline=False)
        e.add_field(name="Tempo usado (não cobra indisponibilidade)", value=fmt_duration(used_seconds), inline=True)
        e.add_field(name="Créditos consumidos", value=money(billed), inline=True)
        if refunded:
            e.add_field(name="Estornado (≤2min)", value=money(refunded), inline=True)
        e.add_field(name="Encerrada em", value=_fmt_dt(now), inline=True)
        e.add_field(name="Saldo atual", value=money(balance), inline=True)
        await notify_user(self.bot, user_id, e)
        log.info("Sessão #%s encerrada (%s). Usado=%ds, consumido=%sc.",
                 session_id, reason, int(used_seconds), billed)
        return True

    async def force_end_session(self, session_id: int) -> dict:
        """Encerra uma sessão running pelo id (usado pelo AdminCog).
        Reutiliza _finish_session: tolerância 2min (refund), resumo por DM.
        Retorna dict com status para quem chamou."""
        ok = await self._finish_session(
            session_id,
            final_status="ended",
            reason="encerrada pela administração",
        )
        if not ok:
            return {
                "status": "not_found_or_already_ended",
                "session_id": session_id,
                "ended": False,
            }
        return {
            "status": "ended",
            "session_id": session_id,
            "ended": True,
        }

    # =======================================================================
    # 6) Histórico  (/sessoes) + métodos expostos
    # =======================================================================

    @app_commands.command(name="sessoes", description="Ver seu histórico de sessões simuladas")
    @not_suspended
    async def cmd_sessoes(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "history"):
            return
        user_id = interaction.user.id
        history = await self.history_summary(user_id, limit=10)
        if not history:
            await interaction.response.send_message(
                embed=info_embed("Sem histórico", "Você ainda não realizou nenhuma sessão simulada."),
                ephemeral=True,
            )
            return
        lines = []
        for h in history:
            status_icon = {"running": "▶️", "ended": "⏹️", "expired": "⌛"}.get(h["status"], "•")
            lines.append(
                f"{status_icon} **#{h['id']}** — {h['video_filename'][:40]} — "
                f"{money(h['billed_cents'])} — {h['status']}"
            )
        e = info_embed("Suas últimas sessões simuladas", "\n".join(lines))
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def active_summary(self, user_id: int) -> list[dict]:
        """Sessões ativas do usuário (para o painel)."""
        with get_db() as con:
            rows = active_sessions(con, user_id)
            out = []
            for r in rows:
                video = con.execute(
                    "SELECT filename FROM videos WHERE id=?", (r["video_id"],)
                ).fetchone()
                out.append({
                    "id": r["id"],
                    "video_id": r["video_id"],
                    "video_filename": video["filename"] if video else f"vídeo #{r['video_id']}",
                    "started_at": r["started_at"],
                    "expires_at": r["expires_at"],
                    "duration_hours": r["duration_hours"],
                    "price_cents": r["price_cents"],
                    "billed_cents": r["billed_cents"],
                    "renewals": r["renewals"],
                })
            return out

    async def history_summary(self, user_id: int, limit: int = 10) -> list[dict]:
        """Histórico recente do usuário (inclui ativas)."""
        with get_db() as con:
            rows = con.execute(
                """SELECT s.*, v.filename AS video_filename
                   FROM sessions s
                   LEFT JOIN videos v ON v.id = s.video_id
                   WHERE s.user_id=?
                   ORDER BY s.started_at DESC
                   LIMIT ?""",
                (user_id, max(1, limit)),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "video_id": r["video_id"],
                    "video_filename": r["video_filename"] or f"vídeo #{r['video_id']}",
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "expires_at": r["expires_at"],
                    "duration_hours": r["duration_hours"],
                    "price_cents": r["price_cents"],
                    "billed_cents": r["billed_cents"],
                    "renewals": r["renewals"],
                    "downtime_seconds": r["downtime_seconds"],
                }
                for r in rows
            ]

    # -- utilidade de resposta (funciona tanto de comando quanto de botão) --
    async def _respond(
        self, interaction: discord.Interaction, emb: discord.Embed, view: Optional[discord.ui.View] = None
    ) -> None:
        kwargs = {"embed": emb, "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SessionsPricesCog(bot))
