"""Cog de usuários e créditos — Hapination Live.

Responsabilidades (especificação SPEC.md seção 2.1):
  - Auto-cadastro (ensure_user) em toda interação do usuário.
  - Apelidos: próprio usuário (/definir_apelido) e proprietário (/admin usuario apelido),
    com filtro básico contra linguagem ofensiva e falsificação de identidade.
  - Suspensão/reativação SÓ pelo proprietário; a checagem de bloqueio fica por conta
    de core.common.is_suspended / decorador not_suspended nos demais cogs. Aqui apenas
    gravamos o status e notificamos o usuário por DM.
  - Créditos: adicionar/remover/corrigir SÓ pelo proprietário. Administradores NÃO podem
    alterar créditos — recebem erro específico. Usa core.services.adjust_credits, que já
    registra balance_after em credit_logs. Usuário é notificado por DM.
  - Histórico de créditos: próprio usuário (/historico_creditos, ephemeral) e admin
    pode consultar qualquer usuário (/admin usuario historico, só visualização).
  - Gestão de administradores SÓ pelo proprietário, na tabela `admins`. O proprietário
    é implícito (env var OWNER_DISCORD_ID) e nunca entra nessa tabela.

Métodos expostos para outros cogs (via bot.get_cog("UsersCredits")):
  - async notify_credit_change(user_id, delta, reason) -> None
  - async is_suspended(user_id) -> bool

Créditos são fictícios. Dados privados (saldo, histórico) SEMPRE em ephemeral ou DM.
"""
from __future__ import annotations

import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.common import (
    check_cooldown,
    error_embed,
    info_embed,
    is_admin,
    is_owner,
    is_suspended as _is_suspended_sync,
    log,
    money,
    notify_user,
    owner_only,
    admin_only,
    success_embed,
    warn_embed,
)
from core.database import ensure_user, get_db
from core.services import adjust_credits, get_balance

# ---------------------------------------------------------------------------
# Filtro básico de apelidos
# ---------------------------------------------------------------------------
# Lista pequena de palavrões (comparação em texto normalizado: minúsculas,
# sem acentos, só alfanuméricos).
_PALAVROES = {
    "puto", "puta", "porra", "caralho", "caraio", "krl", "cu", "bosta",
    "merda", "foda", "fdp", "filhodaputa", "viado", "buceta", "pinto",
    "rola", "arrombado", "cuzao", "pqp", "vsf", "tomarnocu", "boquete",
    "punheta", "xereca", "otario", "imbecil", "retardado", "desgraca",
    "vagabundo", "vagabunda", "escroto", "escrota", "babaca", "trouxa",
}

# Termos que sugerem falsificação de identidade (equipe/proprietário).
# Proibido conter qualquer um deles no apelido normalizado.
_TERMOS_EQUIPE = {
    "admin", "administrador", "proprietario", "dono", "staff", "mod",
    "moderador", "equipe", "suporte", "oficial", "hapination",
}

# Rótulos amigáveis para os tipos de movimentação de crédito.
_TIPOS_LOG = {
    "add": "Adição",
    "remove": "Remoção",
    "correct": "Correção",
    "consume": "Consumo",
    "refund": "Estorno",
    "manual": "Manual",
}


# Mapeamento básico de leetspeak para o filtro (0→o, 1→i, etc.).
# Aplicado apenas na checagem de palavras proibidas, não na comparação de
# unicidade (para não bloquear apelidos legítimos com dígitos).
_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}


def _normalizar(texto: str, leet: bool = False) -> str:
    """Minúsculas, sem acentos, apenas caracteres alfanuméricos.

    Com leet=True, substitui dígitos/símbolos comuns por letras equivalentes
    para dificultar burlas do filtro (ex.: "Caralh0" → "caralho").
    """
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    if leet:
        texto = "".join(_LEET.get(c, c) for c in texto)
    return "".join(c for c in texto if c.isalnum())


def _validar_apelido(apelido: str) -> Optional[str]:
    """Retorna None se o apelido é válido; senão retorna mensagem de erro."""
    apelido = apelido.strip()
    if not (3 <= len(apelido) <= 20):
        return "O apelido deve ter entre **3 e 20 caracteres**."
    norm = _normalizar(apelido)
    if not norm:
        return "O apelido deve conter letras ou números."
    norm_leet = _normalizar(apelido, leet=True)
    for palavra in _PALAVROES:
        if palavra in norm or palavra in norm_leet:
            return "Este apelido contém linguagem ofensiva e não pode ser usado."
    for termo in _TERMOS_EQUIPE:
        if termo in norm or termo in norm_leet:
            return (
                "Este apelido não é permitido: não use termos que sugiram "
                "pertencer à equipe (admin, staff, mod, proprietário, etc.)."
            )
    return None


def _reais_para_centavos(valor: float) -> int:
    """Converte valor em reais (float) para centavos inteiros, com arredondamento."""
    return int(round(float(valor) * 100))


def _formatar_data(created_at: Optional[str]) -> str:
    """Formata 'YYYY-MM-DD HH:MM:SS' (UTC) para 'DD/MM HH:MM'."""
    if not created_at or len(created_at) < 16:
        return "?"
    return f"{created_at[8:10]}/{created_at[5:7]} {created_at[11:16]}"


class UsersCreditsCog(commands.Cog, name="UsersCredits"):
    """Gestão de usuários, apelidos, suspensão, créditos e administradores."""

    # Grupo /admin com subgrupos creditos, usuario e admin (aninhado => /admin admin ...).
    admin = app_commands.Group(
        name="admin", description="Comandos administrativos do Hapination Live."
    )
    creditos = app_commands.Group(
        name="creditos",
        parent=admin,
        description="Alterar créditos de usuários (somente o proprietário).",
    )
    usuario = app_commands.Group(
        name="usuario",
        parent=admin,
        description="Consultar e gerenciar usuários.",
    )
    admins = app_commands.Group(
        name="admin",
        parent=admin,
        description="Gerenciar administradores (somente o proprietário).",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        log.info("UsersCreditsCog carregado.")

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure(user_id: int) -> None:
        """Auto-cadastro: cria o usuário se não existir e atualiza last_seen_at."""
        with get_db() as con:
            ensure_user(con, user_id)

    async def _apelido_em_uso(self, apelido: str, exceto_user_id: int) -> bool:
        """Verifica se o apelido normalizado já pertence a outro usuário (anti-impersonation)."""
        norm = _normalizar(apelido)
        with get_db() as con:
            rows = con.execute(
                "SELECT user_id, nickname FROM users WHERE nickname IS NOT NULL"
            ).fetchall()
        for row in rows:
            if row["user_id"] == exceto_user_id:
                continue
            if _normalizar(row["nickname"] or "") == norm:
                return True
        return False

    def _build_historico_embed(self, user_id: int, titulo: str) -> discord.Embed:
        """Monta embed com as últimas 20 movimentações de crédito de um usuário."""
        with get_db() as con:
            rows = con.execute(
                """
                SELECT * FROM credit_logs
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT 20
                """,
                (user_id,),
            ).fetchall()
            saldo = get_balance(con, user_id)
        if not rows:
            return info_embed(
                titulo,
                "Nenhuma movimentação de crédito registrada ainda.\n"
                f"Saldo atual: **{money(saldo)}** (créditos fictícios).",
            )
        linhas = []
        for r in rows:
            tipo = _TIPOS_LOG.get(r["type"], r["type"] or "Manual")
            sinal = "+" if r["amount_cents"] >= 0 else "-"
            depois = r["balance_after"]
            depois_str = money(depois) if depois is not None else "?"
            motivo = f" — {r['reason']}" if r["reason"] else ""
            linhas.append(
                f"`{_formatar_data(r['created_at'])}` **{tipo}** "
                f"{sinal}{money(abs(r['amount_cents']))} → saldo {depois_str}{motivo}"
            )
        desc = "\n".join(linhas) + f"\n\nSaldo atual: **{money(saldo)}**"
        emb = info_embed(titulo, desc)
        emb.set_footer(text="Créditos fictícios • Hapination Live")
        return emb

    # ------------------------------------------------------------------
    # Métodos expostos para outros cogs
    # ------------------------------------------------------------------
    async def notify_credit_change(self, user_id: int, delta: int, reason: str) -> None:
        """Notifica o usuário por DM sobre uma alteração de créditos.

        `delta` está em centavos (positivo = adicionou, negativo = removeu).
        Nunca lança exceção — falhas são logadas.
        """
        try:
            with get_db() as con:
                saldo = get_balance(con, user_id)
            if delta > 0:
                titulo = "💳 Créditos adicionados"
            elif delta < 0:
                titulo = "💳 Créditos removidos"
            else:
                titulo = "💳 Créditos atualizados"
            sinal = "+" if delta >= 0 else "-"
            desc = (
                f"Alteração: **{sinal}{money(abs(delta))}**\n"
                f"Motivo: {reason or 'Não informado.'}\n"
                f"Saldo atual: **{money(saldo)}**\n\n"
                "_Créditos fictícios — Hapination Live (sessões simuladas)._"
            )
            await notify_user(self.bot, user_id, info_embed(titulo, desc))
        except Exception as exc:
            log.exception(
                "notify_credit_change falhou para user=%s delta=%s: %s",
                user_id, delta, exc,
            )

    async def is_suspended(self, user_id: int) -> bool:
        """Versão async de core.common.is_suspended, para uso por outros cogs."""
        try:
            return _is_suspended_sync(user_id)
        except Exception as exc:
            log.exception("is_suspended falhou para user=%s: %s", user_id, exc)
            return False

    # ------------------------------------------------------------------
    # Métodos públicos chamados pelos atalhos do painel admin (AdminCog).
    # NÃO respondem a interaction (quem responde é o AdminCog). Em caso de
    # erro, levantam exceção com mensagem amigável.
    # ------------------------------------------------------------------
    async def suspend_user(self, interaction, user_id: int, reason: str) -> None:
        """Suspende um usuário. SÓ proprietário (verificado dentro do método).
        Grava status='suspended' + suspended_reason e notifica o usuário por DM."""
        if not is_owner(interaction.user.id):
            raise PermissionError("Apenas o proprietário pode suspender usuários.")
        if is_owner(user_id):
            raise ValueError("O proprietário não pode ser suspenso.")
        self._ensure(user_id)
        motivo = (reason or "").strip() or "Não informado."
        with get_db() as con:
            row = con.execute(
                "SELECT status FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row and row["status"] == "suspended":
                raise RuntimeError("O usuário já está suspenso.")
            con.execute(
                "UPDATE users SET status='suspended', suspended_reason=? WHERE user_id=?",
                (motivo, user_id),
            )
        # O bloqueio de ações (sessão, vídeo, ticket) é feito via
        # core.common.is_suspended / decorador not_suspended nos demais cogs.
        await notify_user(
            self.bot,
            user_id,
            error_embed(
                "Conta suspensa",
                f"Sua conta no Hapination Live foi suspensa.\n\n"
                f"**Motivo:** {motivo}\n\n"
                f"Você não poderá iniciar sessões, enviar vídeos ou abrir "
                f"atendimentos. Procure o proprietário para mais informações.",
            ),
        )
        log.info("Usuário %s suspenso por %s (motivo: %s).", user_id, interaction.user.id, motivo)

    async def reactivate_user(self, interaction, user_id: int) -> None:
        """Reativa um usuário suspenso. SÓ proprietário.
        Grava status='active' e notifica o usuário por DM."""
        if not is_owner(interaction.user.id):
            raise PermissionError("Apenas o proprietário pode reativar usuários.")
        self._ensure(user_id)
        with get_db() as con:
            row = con.execute(
                "SELECT status FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row or row["status"] != "suspended":
                raise RuntimeError("O usuário não está suspenso.")
            con.execute(
                "UPDATE users SET status='active', suspended_reason=NULL WHERE user_id=?",
                (user_id,),
            )
        await notify_user(
            self.bot,
            user_id,
            success_embed(
                "Conta reativada",
                "Sua conta no Hapination Live foi reativada. Você já pode "
                "iniciar sessões, enviar vídeos e abrir atendimentos novamente.",
            ),
        )
        log.info("Usuário %s reativado por %s.", user_id, interaction.user.id)

    async def set_nickname(self, interaction, user_id: int, nickname: str) -> None:
        """Altera o apelido de qualquer usuário. SÓ proprietário.
        Aplica o mesmo filtro (3–20 chars, palavrões, leetspeak, termos de
        equipe, anti-impersonation), grava e notifica o usuário por DM."""
        if not is_owner(interaction.user.id):
            raise PermissionError("Apenas o proprietário pode alterar apelidos de outros usuários.")
        erro = _validar_apelido(nickname)
        if erro:
            raise ValueError(erro)
        if await self._apelido_em_uso(nickname, user_id):
            raise RuntimeError("Este apelido já está sendo usado por outro usuário.")
        apelido = nickname.strip()
        self._ensure(user_id)
        with get_db() as con:
            con.execute(
                "UPDATE users SET nickname=? WHERE user_id=?",
                (apelido, user_id),
            )
        await notify_user(
            self.bot,
            user_id,
            info_embed(
                "Apelido alterado pela administração",
                f"Seu apelido no Hapination Live agora é **{apelido}**.",
            ),
        )
        log.info("Apelido do usuário %s alterado para %s por %s.", user_id, apelido, interaction.user.id)

    # ------------------------------------------------------------------
    # Comandos de nível superior (qualquer usuário)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="definir_apelido",
        description="Define ou altera o seu apelido (3 a 20 caracteres).",
    )
    @app_commands.describe(apelido="Seu novo apelido (3 a 20 caracteres).")
    async def definir_apelido(self, interaction: discord.Interaction, apelido: str):
        self._ensure(interaction.user.id)
        if not await check_cooldown(interaction, "definir_apelido"):
            return
        erro = _validar_apelido(apelido)
        if erro:
            await interaction.response.send_message(
                embed=error_embed("Apelido inválido", erro), ephemeral=True
            )
            return
        if await self._apelido_em_uso(apelido, interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed(
                    "Apelido em uso",
                    "Este apelido já está sendo usado por outro usuário. "
                    "Escolha outro para evitar confusão de identidade.",
                ),
                ephemeral=True,
            )
            return
        with get_db() as con:
            con.execute(
                "UPDATE users SET nickname=? WHERE user_id=?",
                (apelido.strip(), interaction.user.id),
            )
        await interaction.response.send_message(
            embed=success_embed(
                "Apelido atualizado",
                f"Seu apelido agora é **{apelido.strip()}**.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="historico_creditos",
        description="Veja suas últimas 20 movimentações de crédito (privado).",
    )
    async def historico_creditos(self, interaction: discord.Interaction):
        self._ensure(interaction.user.id)
        emb = self._build_historico_embed(
            interaction.user.id, "📜 Seu histórico de créditos"
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)

    # ------------------------------------------------------------------
    # /admin creditos adicionar|remover|corrigir  (SÓ proprietário)
    # ------------------------------------------------------------------
    async def _creditos_permitido(self, interaction: discord.Interaction) -> bool:
        """Erro ESPECÍFICO: administradores NÃO alteram créditos, só o proprietário."""
        if is_owner(interaction.user.id):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Somente o proprietário pode alterar créditos",
                "A alteração de créditos (adicionar, remover ou corrigir) é "
                "exclusiva do proprietário do bot. Administradores podem apenas "
                "**consultar** saldos e históricos em `/admin usuario ver` e "
                "`/admin usuario historico`.",
            ),
            ephemeral=True,
        )
        return False

    @creditos.command(name="adicionar", description="Adiciona créditos a um usuário (proprietário).")
    @app_commands.describe(
        usuario="Usuário que receberá os créditos.",
        valor="Valor em reais (ex: 10 ou 10.50).",
        motivo="Motivo opcional da alteração.",
    )
    async def creditos_adicionar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        valor: float,
        motivo: Optional[str] = None,
    ):
        if not await self._creditos_permitido(interaction):
            return
        if not await check_cooldown(interaction, "creditos_alterar"):
            return
        cents = _reais_para_centavos(valor)
        if cents <= 0:
            await interaction.response.send_message(
                embed=error_embed("Valor inválido", "Informe um valor positivo em reais."),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            novo_saldo = adjust_credits(
                con,
                usuario.id,
                cents,
                actor_id=interaction.user.id,
                log_type="add",
                reason=motivo,
            )
        await self.notify_credit_change(usuario.id, cents, motivo or "Adição manual")
        await interaction.response.send_message(
            embed=success_embed(
                "Créditos adicionados",
                f"**+{money(cents)}** para {usuario.mention} (`{usuario.id}`).\n"
                f"Novo saldo: **{money(novo_saldo)}**.\n"
                f"Usuário notificado por DM.",
            ),
            ephemeral=True,
        )

    @creditos.command(name="remover", description="Remove créditos de um usuário (proprietário).")
    @app_commands.describe(
        usuario="Usuário que terá créditos removidos.",
        valor="Valor em reais (ex: 10 ou 10.50).",
        motivo="Motivo opcional da alteração.",
    )
    async def creditos_remover(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        valor: float,
        motivo: Optional[str] = None,
    ):
        if not await self._creditos_permitido(interaction):
            return
        if not await check_cooldown(interaction, "creditos_alterar"):
            return
        cents = _reais_para_centavos(valor)
        if cents <= 0:
            await interaction.response.send_message(
                embed=error_embed("Valor inválido", "Informe um valor positivo em reais."),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            saldo_atual = get_balance(con, usuario.id)
            if saldo_atual < cents:
                await interaction.response.send_message(
                    embed=error_embed(
                        "Saldo insuficiente",
                        f"O usuário tem **{money(saldo_atual)}** e não é possível "
                        f"remover **{money(cents)}** (o saldo não pode ficar negativo).",
                    ),
                    ephemeral=True,
                )
                return
            novo_saldo = adjust_credits(
                con,
                usuario.id,
                -cents,
                actor_id=interaction.user.id,
                log_type="remove",
                reason=motivo,
            )
        await self.notify_credit_change(usuario.id, -cents, motivo or "Remoção manual")
        await interaction.response.send_message(
            embed=success_embed(
                "Créditos removidos",
                f"**-{money(cents)}** de {usuario.mention} (`{usuario.id}`).\n"
                f"Novo saldo: **{money(novo_saldo)}**.\n"
                f"Usuário notificado por DM.",
            ),
            ephemeral=True,
        )

    @creditos.command(name="corrigir", description="Corrige o saldo de um usuário para um valor exato (proprietário).")
    @app_commands.describe(
        usuario="Usuário cujo saldo será corrigido.",
        valor="Novo saldo exato em reais (não pode ser negativo).",
        motivo="Motivo opcional da correção.",
    )
    async def creditos_corrigir(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        valor: float,
        motivo: Optional[str] = None,
    ):
        if not await self._creditos_permitido(interaction):
            return
        if not await check_cooldown(interaction, "creditos_alterar"):
            return
        cents = _reais_para_centavos(valor)
        if cents < 0:
            await interaction.response.send_message(
                embed=error_embed(
                    "Valor inválido", "O saldo corrigido não pode ser negativo."
                ),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            saldo_atual = get_balance(con, usuario.id)
            delta = cents - saldo_atual
            novo_saldo = adjust_credits(
                con,
                usuario.id,
                delta,
                actor_id=interaction.user.id,
                log_type="correct",
                reason=motivo,
            )
        await self.notify_credit_change(
            usuario.id, delta, motivo or "Correção de saldo"
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Saldo corrigido",
                f"Saldo de {usuario.mention} (`{usuario.id}`) definido para "
                f"**{money(novo_saldo)}** (variação: "
                f"{'+' if delta >= 0 else '-'}{money(abs(delta))}).\n"
                f"Usuário notificado por DM.",
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /admin usuario apelido|suspender|reativar|ver|historico
    # ------------------------------------------------------------------
    @usuario.command(name="apelido", description="Altera o apelido de qualquer usuário (proprietário).")
    @app_commands.describe(
        usuario="Usuário cujo apelido será alterado.",
        apelido="Novo apelido (3 a 20 caracteres).",
    )
    @owner_only
    async def usuario_apelido(
        self, interaction: discord.Interaction, usuario: discord.User, apelido: str
    ):
        if not await check_cooldown(interaction, "admin_apelido"):
            return
        erro = _validar_apelido(apelido)
        if erro:
            await interaction.response.send_message(
                embed=error_embed("Apelido inválido", erro), ephemeral=True
            )
            return
        if await self._apelido_em_uso(apelido, usuario.id):
            await interaction.response.send_message(
                embed=error_embed(
                    "Apelido em uso",
                    "Este apelido já está sendo usado por outro usuário.",
                ),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            con.execute(
                "UPDATE users SET nickname=? WHERE user_id=?",
                (apelido.strip(), usuario.id),
            )
        await interaction.response.send_message(
            embed=success_embed(
                "Apelido atualizado",
                f"Apelido de {usuario.mention} (`{usuario.id}`) alterado para "
                f"**{apelido.strip()}**.",
            ),
            ephemeral=True,
        )

    @usuario.command(name="suspender", description="Suspende um usuário (proprietário).")
    @app_commands.describe(
        usuario="Usuário a ser suspenso.",
        motivo="Motivo da suspensão (será enviado ao usuário por DM).",
    )
    @owner_only
    async def usuario_suspender(
        self, interaction: discord.Interaction, usuario: discord.User, motivo: str
    ):
        if not await check_cooldown(interaction, "suspender"):
            return
        if is_owner(usuario.id):
            await interaction.response.send_message(
                embed=error_embed(
                    "Operação inválida",
                    "O proprietário não pode ser suspenso.",
                ),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            row = con.execute(
                "SELECT status FROM users WHERE user_id=?", (usuario.id,)
            ).fetchone()
            if row and row["status"] == "suspended":
                await interaction.response.send_message(
                    embed=warn_embed(
                        "Já suspenso",
                        f"{usuario.mention} já está suspenso. Use "
                        f"`/admin usuario reativar` para reativar.",
                    ),
                    ephemeral=True,
                )
                return
            con.execute(
                "UPDATE users SET status='suspended', suspended_reason=? WHERE user_id=?",
                (motivo, usuario.id),
            )
        # Notifica o usuário por DM. O bloqueio de ações (sessão, vídeo, ticket)
        # é feito via core.common.is_suspended / decorador not_suspended nos
        # demais cogs — aqui apenas gravamos o status e notificamos.
        dm_ok = await notify_user(
            self.bot,
            usuario.id,
            error_embed(
                "Conta suspensa",
                f"Sua conta no Hapination Live foi suspensa.\n\n"
                f"**Motivo:** {motivo}\n\n"
                f"Você não poderá iniciar sessões, enviar vídeos ou abrir "
                f"atendimentos. Procure o proprietário para mais informações.",
            ),
        )
        aviso_dm = "Usuário notificado por DM." if dm_ok else "Não foi possível enviar DM ao usuário (DMs fechadas)."
        await interaction.response.send_message(
            embed=success_embed(
                "Usuário suspenso",
                f"{usuario.mention} (`{usuario.id}`) foi suspenso.\n"
                f"**Motivo:** {motivo}\n{aviso_dm}",
            ),
            ephemeral=True,
        )

    @usuario.command(name="reativar", description="Reativa um usuário suspenso (proprietário).")
    @app_commands.describe(usuario="Usuário a ser reativado.")
    @owner_only
    async def usuario_reativar(
        self, interaction: discord.Interaction, usuario: discord.User
    ):
        if not await check_cooldown(interaction, "reativar"):
            return
        self._ensure(usuario.id)
        with get_db() as con:
            row = con.execute(
                "SELECT status FROM users WHERE user_id=?", (usuario.id,)
            ).fetchone()
            if not row or row["status"] != "suspended":
                await interaction.response.send_message(
                    embed=warn_embed(
                        "Não está suspenso",
                        f"{usuario.mention} já está com a conta ativa.",
                    ),
                    ephemeral=True,
                )
                return
            con.execute(
                "UPDATE users SET status='active', suspended_reason=NULL WHERE user_id=?",
                (usuario.id,),
            )
        dm_ok = await notify_user(
            self.bot,
            usuario.id,
            success_embed(
                "Conta reativada",
                "Sua conta no Hapination Live foi reativada. Você já pode "
                "iniciar sessões, enviar vídeos e abrir atendimentos novamente.",
            ),
        )
        aviso_dm = "Usuário notificado por DM." if dm_ok else "Não foi possível enviar DM ao usuário (DMs fechadas)."
        await interaction.response.send_message(
            embed=success_embed(
                "Usuário reativado",
                f"{usuario.mention} (`{usuario.id}`) foi reativado.\n{aviso_dm}",
            ),
            ephemeral=True,
        )

    @usuario.command(name="ver", description="Visualiza dados de um usuário (equipe, somente leitura).")
    @app_commands.describe(usuario="Usuário a ser consultado.")
    @admin_only
    async def usuario_ver(
        self, interaction: discord.Interaction, usuario: discord.User
    ):
        self._ensure(usuario.id)
        with get_db() as con:
            row = con.execute(
                "SELECT * FROM users WHERE user_id=?", (usuario.id,)
            ).fetchone()
        if not row:
            await interaction.response.send_message(
                embed=error_embed("Usuário não encontrado", "Este usuário não está cadastrado."),
                ephemeral=True,
            )
            return
        status = "✅ Ativo" if row["status"] != "suspended" else "⛔ Suspenso"
        motivo_susp = row["suspended_reason"] or "—"
        apelido = row["nickname"] or "Não definido"
        saldo = money(row["credits_cents"] or 0)
        eh_admin = is_admin(usuario.id)
        eh_dono = is_owner(usuario.id)
        cargo = "👑 Proprietário" if eh_dono else ("🛡️ Administrador" if eh_admin else "👤 Usuário")
        desc = (
            f"**Usuário:** {usuario.mention} (`{usuario.id}`)\n"
            f"**Apelido:** {apelido}\n"
            f"**Cargo:** {cargo}\n"
            f"**Status:** {status}\n"
            f"**Motivo da suspensão:** {motivo_susp}\n"
            f"**Saldo de créditos:** {saldo} (fictícios)\n"
            f"**Cadastrado em:** {_formatar_data(row['created_at'])}\n"
            f"**Visto por último:** {_formatar_data(row['last_seen_at'])}"
        )
        await interaction.response.send_message(
            embed=info_embed("👤 Dados do usuário", desc), ephemeral=True
        )

    @usuario.command(name="historico", description="Histórico de créditos de qualquer usuário (equipe, só leitura).")
    @app_commands.describe(usuario="Usuário a ser consultado.")
    @admin_only
    async def usuario_historico(
        self, interaction: discord.Interaction, usuario: discord.User
    ):
        self._ensure(usuario.id)
        emb = self._build_historico_embed(
            usuario.id, f"📜 Histórico de créditos — {usuario}"
        )
        await interaction.response.send_message(embed=emb, ephemeral=True)

    # ------------------------------------------------------------------
    # /admin admin adicionar|remover|listar  (SÓ proprietário)
    # ------------------------------------------------------------------
    @admins.command(name="adicionar", description="Promove um usuário a administrador (proprietário).")
    @app_commands.describe(usuario="Usuário a ser promovido.")
    @owner_only
    async def admin_adicionar(
        self, interaction: discord.Interaction, usuario: discord.User
    ):
        if not await check_cooldown(interaction, "admin_gestao"):
            return
        if is_owner(usuario.id):
            await interaction.response.send_message(
                embed=warn_embed(
                    "Proprietário implícito",
                    f"{usuario.mention} é o proprietário e já tem todos os "
                    f"privilégios — não entra na tabela de administradores.",
                ),
                ephemeral=True,
            )
            return
        self._ensure(usuario.id)
        with get_db() as con:
            existe = con.execute(
                "SELECT 1 FROM admins WHERE user_id=?", (usuario.id,)
            ).fetchone()
            if existe:
                await interaction.response.send_message(
                    embed=warn_embed(
                        "Já é administrador",
                        f"{usuario.mention} já está na lista de administradores.",
                    ),
                    ephemeral=True,
                )
                return
            con.execute(
                "INSERT INTO admins(user_id, added_by) VALUES (?, ?)",
                (usuario.id, interaction.user.id),
            )
        await notify_user(
            self.bot,
            usuario.id,
            success_embed(
                "Você é administrador",
                "Você foi promovido a administrador do Hapination Live. "
                "Lembre-se: **administradores não podem alterar créditos** — "
                "isso é exclusivo do proprietário.",
            ),
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Administrador adicionado",
                f"{usuario.mention} (`{usuario.id}`) agora é administrador.",
            ),
            ephemeral=True,
        )

    @admins.command(name="remover", description="Remove um administrador (proprietário).")
    @app_commands.describe(usuario="Administrador a ser removido.")
    @owner_only
    async def admin_remover(
        self, interaction: discord.Interaction, usuario: discord.User
    ):
        if not await check_cooldown(interaction, "admin_gestao"):
            return
        if is_owner(usuario.id):
            await interaction.response.send_message(
                embed=error_embed(
                    "Operação inválida",
                    "O proprietário é implícito e não pode ser removido da equipe.",
                ),
                ephemeral=True,
            )
            return
        with get_db() as con:
            cur = con.execute(
                "DELETE FROM admins WHERE user_id=?", (usuario.id,)
            )
            removidos = cur.rowcount
        if removidos == 0:
            await interaction.response.send_message(
                embed=warn_embed(
                    "Não é administrador",
                    f"{usuario.mention} não está na lista de administradores.",
                ),
                ephemeral=True,
            )
            return
        # A tabela admins é a fonte de verdade; revoga também as ACLs
        # individuais que o bot concedeu nos canais restritos.
        panel_cog = self.bot.get_cog("PanelCog")
        acl_failures: list[str] = []
        if panel_cog is not None:
            acl_failures = await panel_cog.revoke_staff_permissions(usuario.id)
        else:
            acl_failures = ["PanelCog indisponível; ACLs do Discord não verificadas"]
            log.error(
                "UsersCredits: PanelCog indisponível ao revogar ACL do usuário %s",
                usuario.id,
            )
        await notify_user(
            self.bot,
            usuario.id,
            info_embed(
                "Privilégios removidos",
                "Seus privilégios de administrador do Hapination Live foram "
                "removidos pelo proprietário.",
            ),
        )
        if acl_failures:
            await interaction.response.send_message(
                embed=warn_embed(
                    "Administrador removido; revisão de acesso pendente",
                    f"{usuario.mention} não é mais administrador no banco. "
                    "Não foi possível confirmar a revogação de todas as permissões "
                    "individuais no Discord. Verifique os canais avisos/administração "
                    f"nas guildas: {', '.join(acl_failures)}.",
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=success_embed(
                    "Administrador removido",
                    f"{usuario.mention} (`{usuario.id}`) não é mais administrador "
                    "e as permissões individuais gerenciadas foram revogadas.",
                ),
                ephemeral=True,
            )

    @admins.command(name="listar", description="Lista todos os administradores (proprietário).")
    @owner_only
    async def admin_listar(self, interaction: discord.Interaction):
        with get_db() as con:
            rows = con.execute(
                """
                SELECT a.user_id, a.added_by, a.created_at, u.nickname
                FROM admins a
                LEFT JOIN users u ON u.user_id = a.user_id
                ORDER BY a.created_at ASC
                """
            ).fetchall()
        linhas = [f"👑 Proprietário: <@{config.OWNER_DISCORD_ID}> (implícito)"]
        if not rows:
            linhas.append("_Nenhum administrador adicional cadastrado._")
        else:
            for r in rows:
                apelido = f" ({r['nickname']})" if r["nickname"] else ""
                linhas.append(
                    f"🛡️ <@{r['user_id']}>{apelido} • adicionado por <@{r['added_by']}> "
                    f"em {_formatar_data(r['created_at'])}"
                )
        await interaction.response.send_message(
            embed=info_embed("🛡️ Equipe administrativa", "\n".join(linhas)),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UsersCreditsCog(bot))
