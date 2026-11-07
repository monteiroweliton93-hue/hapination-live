"""Cog de vídeos do Hapination Live — upload, listagem, exclusão e limpeza.

Responsabilidades (especificação SPEC.md seção 2.3):
  - Upload por comando `/enviar_video` (Attachment) e por "modo upload" no canal
    privado do usuário (flag em memória com timeout de 5 min, listener on_message).
  - Validação de tipo/tamanho, controle de armazenamento por usuário, checagem
    de manutenção e suspensão, salvamento DIRETO no destino final (sem temp).
  - Miniaturas extraídas com ffmpeg SOMENTE se o binário estiver no PATH
    (shutil.which); caso contrário, pula silenciosamente e thumbnail_path fica NULL.
  - Listagem própria (`/meus_videos`) e método exposto `list_summary`.
  - Exclusão manual com View de confirmação (2 botões); admin/proprietário podem
    excluir qualquer vídeo.
  - Exclusão automática por retenção (`cleanup_old`, chamado pelo loop de
    background em outro cog) + limpeza de arquivos órfãos.

Acesso: proprietário, dono do vídeo e admins. UI em português. Dados privados
sempre em ephemeral ou canal privado. NÃO modifica core/, bot.py, SPEC.md.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core import config
from core.common import (
    check_cooldown,
    error_embed,
    info_embed,
    is_admin,
    is_suspended,
    log,
    not_suspended,
    notify_user,
    success_embed,
    warn_embed,
    embed,
)
from core.database import ensure_user, get_db, get_int_setting
from core.services import maintenance_message, maintenance_on

# Extensões de vídeo aceitas como fallback quando o Discord não envia content_type.
_VIDEO_EXTS = (
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
    ".wmv", ".flv", ".mpeg", ".mpg", ".3gp",
)

# Duração da janela de "modo upload" (5 minutos, conforme especificação).
_UPLOAD_MODE_SECONDS = 300

# Limite de vídeos mostrados no embed de listagem (Discord suporta até 25 fields).
_LIST_LIMIT = 25


def _fmt_bytes(n: int) -> str:
    """Formata bytes em string legível (B/KB/MB)."""
    n = max(0, int(n))
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _safe_filename(filename: str) -> str:
    """Sanitiza nome de arquivo: remove path traversal e caracteres de risco."""
    name = Path(filename or "video").name
    stem, dot, ext = name.rpartition(".")
    if not stem:  # arquivo sem nome, só extensão (ex: ".mp4")
        stem, dot, ext = "video", "", name.lstrip(".")
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:60].strip("_") or "video"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:10]
    return f"{stem}.{ext}" if ext else stem


def _is_video_attachment(att: discord.Attachment) -> bool:
    """Válida pelo content_type (primário) e pela extensão (fallback)."""
    ct = (att.content_type or "").lower()
    if ct.startswith("video/"):
        return True
    return Path(att.filename or "").suffix.lower() in _VIDEO_EXTS


def _is_private_panel_channel(channel: discord.abc.Messageable) -> bool:
    """Detecta canal de painel privado pelo nome (normalizado) ou pelo tópico.

    O Discord remove emojis e caracteres especiais dos nomes de canal, então
    `👤・painel-<id>` vira `painel-<id>`. Exigimos que o último segmento seja
    numérico (o user_id) para evitar falsos positivos com outros canais.
    """
    if not isinstance(channel, discord.TextChannel):
        return False
    name = channel.name or ""
    if name.startswith("👤・painel-"):
        return True
    norm = re.sub(r"[^a-z0-9-]", "", name.lower())
    parts = norm.split("-")
    if parts and parts[0] == "painel" and parts[-1].isdigit():
        return True
    topic = (channel.topic or "").lower()
    return "painel" in topic and "hapination" in topic


class _DeleteConfirmView(discord.ui.View):
    """View de confirmação de exclusão (2 botões). Transiente (timeout=60s)."""

    def __init__(self, cog: "VideosCog", video_id: int, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.video_id = video_id
        self.requester_id = requester_id

    def _disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def _check_requester(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Não autorizado",
                    "Apenas quem solicitou a exclusão pode confirmar ou cancelar.",
                ),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirmar exclusão",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="hap_vid_delete_confirm",
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._check_requester(interaction):
            return
        self._disable_all()
        row = await self.cog.fetch_video(self.video_id)
        if row is None or row["deleted"]:
            await interaction.response.edit_message(
                embed=error_embed(
                    "Vídeo não encontrado",
                    f"O vídeo #{self.video_id} já foi excluído ou não existe.",
                ),
                view=self,
            )
            self.stop()
            return
        try:
            await self.cog.soft_delete(row, "exclusão manual")
            result = success_embed(
                "Vídeo excluído",
                f"**#{row['id']} — {row['filename']}** foi removido do armazenamento.",
            )
        except Exception:
            log.exception("Falha ao excluir vídeo %s", self.video_id)
            result = error_embed(
                "Erro ao excluir",
                "Não foi possível remover o vídeo. A equipe foi notificada.",
            )
        await interaction.response.edit_message(embed=result, view=self)
        self.stop()

    @discord.ui.button(
        label="Cancelar",
        style=discord.ButtonStyle.secondary,
        custom_id="hap_vid_delete_cancel",
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._check_requester(interaction):
            return
        self._disable_all()
        await interaction.response.edit_message(
            embed=info_embed("Exclusão cancelada", "O vídeo foi mantido."),
            view=self,
        )
        self.stop()


class VideosCog(commands.Cog, name="VideosCog"):
    """Gestão de vídeos: upload, listagem, exclusão e limpeza automática."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user_id -> timestamp de expiração do modo upload (time.time() + 300).
        self._upload_mode: dict[int, float] = {}
        # Caminho do ffmpeg OU None se indisponível (checado uma vez no init).
        self._ffmpeg: str | None = shutil.which("ffmpeg")
        if self._ffmpeg:
            log.info("VideosCog: ffmpeg detectado em %s — miniaturas habilitadas.", self._ffmpeg)
        else:
            log.info("VideosCog: ffmpeg não encontrado no PATH — miniaturas desabilitadas (thumbnail_path=NULL).")

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------
    async def fetch_video(self, video_id: int):
        """Retorna a linha do vídeo (sqlite3.Row) ou None."""
        with get_db() as con:
            return con.execute(
                "SELECT * FROM videos WHERE id=?", (video_id,)
            ).fetchone()

    async def soft_delete(self, row, reason: str) -> None:
        """Marca deleted=1, remove arquivo + miniatura do disco e notifica o dono.

        Não faz checagem de permissão — o chamador deve autorizar antes.
        Idempotente: o UPDATE só afeta linhas com deleted=0.
        """
        vid = row["id"]
        uid = row["user_id"]
        fname = row["filename"]
        with get_db() as con:
            con.execute(
                "UPDATE videos SET deleted=1, deleted_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND deleted=0",
                (vid,),
            )
        # Remove arquivo do vídeo e miniatura. Falha de disco não interrompe.
        for key in ("path", "thumbnail_path"):
            p = row[key]
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
            except Exception as exc:
                log.warning("Não foi possível remover arquivo %s: %s", p, exc)
        emb = warn_embed(
            "Vídeo removido",
            f"**#{vid} — {fname}** foi removido.\nMotivo: {reason}.",
        )
        await notify_user(self.bot, uid, emb)

    async def _extract_thumbnail(self, video_path: Path, video_id: int) -> str | None:
        """Extrai um frame em 1s para data/thumbnails/<id>.jpg. Retorna path ou None.

        Só roda se ffmpeg estiver disponível; qualquer falha é silenciosa
        (log de aviso) e thumbnail_path permanece NULL.
        """
        if not self._ffmpeg:
            return None
        out = config.THUMBNAIL_DIR / f"{video_id}.jpg"
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-y",
                "-ss", "1",          # seek rápido para 1s
                "-i", str(video_path),
                "-frames:v", "1",    # um único frame
                "-q:v", "3",         # qualidade JPEG alta
                "-vf", "scale=320:-1",  # largura fixa, proporção preservada
                "-an",
                str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            log.warning("Timeout ao extrair miniatura do vídeo %s", video_id)
            return None
        except Exception as exc:
            log.warning("Falha ao extrair miniatura do vídeo %s: %s", video_id, exc)
            return None
        if out.exists() and out.stat().st_size > 0:
            return str(out)
        return None

    async def _import_video(
        self, user: discord.abc.User, attachment: discord.Attachment
    ) -> tuple[bool, str, int | None]:
        """Fluxo único de importação (usado por /enviar_video e pelo on_message).

        Retorna (sucesso, mensagem_para_o_usuário, video_id_ou_None).
        Salva o arquivo DIRETO no destino final, sem arquivo temporário.
        """
        uid = user.id

        # 1) Tipo de arquivo.
        if not _is_video_attachment(attachment):
            return (
                False,
                f"O arquivo `{attachment.filename}` não é um vídeo válido.",
                None,
            )

        # 2) Checagens de banco: manutenção, limites, armazenamento usado.
        with get_db() as con:
            ensure_user(con, uid)
            if maintenance_on(con):
                return (False, maintenance_message(con), None)
            max_mb = get_int_setting(con, "max_upload_mb", config.DEFAULT_MAX_UPLOAD_MB)
            storage_mb = get_int_setting(con, "user_storage_mb", 500)
            used_bytes = con.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS s FROM videos "
                "WHERE user_id=? AND deleted=0",
                (uid,),
            ).fetchone()["s"]

        # 3) Suspensão (checada separadamente; para slash já há decorator, mas o
        #    caminho do on_message precisa desta proteção).
        if is_suspended(uid):
            return (
                False,
                "Sua conta está suspensa e não pode enviar vídeos no momento.",
                None,
            )

        # 4) Limites de tamanho e armazenamento por usuário.
        max_bytes = max_mb * 1024 * 1024
        if attachment.size > max_bytes:
            return (
                False,
                f"Arquivo muito grande: **{_fmt_bytes(attachment.size)}** "
                f"(máximo permitido: {max_mb} MB).",
                None,
            )
        storage_bytes = storage_mb * 1024 * 1024
        if used_bytes + attachment.size > storage_bytes:
            return (
                False,
                f"Armazenamento esgotado: você usa **{_fmt_bytes(used_bytes)}** "
                f"de {storage_mb} MB. Exclua vídeos antigos antes de enviar novos.",
                None,
            )

        # 5) Registra no banco para obter o id, então salva direto no destino final.
        safe = _safe_filename(attachment.filename)
        with get_db() as con:
            cur = con.execute(
                "INSERT INTO videos(user_id, filename, path, size_bytes, "
                "mime_type, deleted) VALUES (?, ?, ?, ?, ?, 0)",
                (uid, safe, "", attachment.size, attachment.content_type or ""),
            )
            video_id = cur.lastrowid
            final_path = config.UPLOAD_DIR / str(uid) / f"{video_id}_{safe}"
            con.execute("UPDATE videos SET path=? WHERE id=?", (str(final_path), video_id))

        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            # attachment.save baixa direto para o destino — sem arquivo temporário.
            await attachment.save(str(final_path))
        except Exception:
            log.exception("Falha ao salvar arquivo do vídeo %s", video_id)
            with get_db() as con:
                con.execute("DELETE FROM videos WHERE id=?", (video_id,))
            return (False, "Falha interna ao salvar o arquivo. Tente novamente.", None)

        # 6) Miniatura (opcional, só com ffmpeg; falha silenciosa).
        thumb = await self._extract_thumbnail(final_path, video_id)
        if thumb:
            with get_db() as con:
                con.execute(
                    "UPDATE videos SET thumbnail_path=? WHERE id=?", (thumb, video_id)
                )

        # 7) Notifica o dono por DM.
        emb = success_embed(
            "Vídeo recebido",
            f"**{safe}** ({_fmt_bytes(attachment.size)}) foi registrado como "
            f"**#{video_id}** e já está disponível para suas sessões simuladas.",
        )
        await notify_user(self.bot, uid, emb)

        return (True, f"Vídeo **#{video_id}** enviado com sucesso.", video_id)

    def _clean_orphans_sync(self) -> None:
        """Remove arquivos em disco sem registro (uploads e miniaturas).

        Roda em thread (asyncio.to_thread) para não bloquear o loop.
        Cuidadoso: só remove arquivos cujo path absoluto não aparece em
        NENHUM registro da tabela videos (ativos ou deletados).
        """
        with get_db() as con:
            rows = con.execute("SELECT path, thumbnail_path FROM videos").fetchall()
        known: set[str] = set()
        for r in rows:
            for key in ("path", "thumbnail_path"):
                p = r[key]
                if not p:
                    continue
                try:
                    known.add(str(Path(p).resolve()))
                except Exception:
                    pass
        for base in (config.UPLOAD_DIR, config.THUMBNAIL_DIR):
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    if str(f.resolve()) not in known:
                        f.unlink(missing_ok=True)
                        log.info("Removido arquivo órfão: %s", f)
                except Exception as exc:
                    log.warning("Falha ao remover arquivo órfão %s: %s", f, exc)

    # ------------------------------------------------------------------
    # Métodos expostos (contrato com outros cogs)
    # ------------------------------------------------------------------
    async def list_summary(self, user_id: int) -> list[dict]:
        """Resumo dos vídeos ativos do usuário (para o painel privado)."""
        with get_db() as con:
            ensure_user(con, user_id)
            rows = con.execute(
                "SELECT id, filename, size_bytes, mime_type, created_at, "
                "thumbnail_path FROM videos WHERE user_id=? AND deleted=0 "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "filename": r["filename"],
                "size_bytes": r["size_bytes"],
                "size_fmt": _fmt_bytes(r["size_bytes"]),
                "mime_type": r["mime_type"],
                "created_at": r["created_at"],
                "thumbnail_path": r["thumbnail_path"],
                "has_thumbnail": bool(r["thumbnail_path"]),
            }
            for r in rows
        ]

    async def cleanup_old(self) -> int:
        """Exclui vídeos que excederam a retenção e limpa órfãos. Retorna qtd excluída.

        O loop que chama este método fica em outro cog (support_background).
        """
        with get_db() as con:
            hours = get_int_setting(
                con, "video_retention_hours", config.DEFAULT_VIDEO_RETENTION_HOURS
            )
            expired = con.execute(
                "SELECT * FROM videos WHERE deleted=0 "
                "AND created_at <= datetime('now', ?)",
                (f"-{hours} hours",),
            ).fetchall()

        count = 0
        for row in expired:
            try:
                await self.soft_delete(
                    row, f"excedeu o prazo de retenção de {hours}h"
                )
                count += 1
            except Exception:
                log.exception("Falha ao limpar vídeo expirado %s", row["id"])

        # Registros deletados cujo arquivo ainda existe: a limpeza de órfãos abaixo
        # já cobre este caso, mas fazemos a passagem explícita (idempotente).
        with get_db() as con:
            deleted_rows = con.execute(
                "SELECT path, thumbnail_path FROM videos WHERE deleted=1"
            ).fetchall()
        for r in deleted_rows:
            for key in ("path", "thumbnail_path"):
                p = r[key]
                if not p:
                    continue
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception as exc:
                    log.warning("Não foi possível remover arquivo de vídeo deletado %s: %s", p, exc)

        # Arquivos em disco sem registro (opcional, cuidadoso, em thread).
        try:
            await asyncio.to_thread(self._clean_orphans_sync)
        except Exception:
            log.exception("Falha na limpeza de arquivos órfãos")

        return count

    async def enable_upload_mode(self, interaction: discord.Interaction) -> None:
        """Ativa o modo upload para o usuário (5 min) e responde ephemeral.

        Chamado pelo botão "📤 Enviar vídeo" do painel privado. O anexo é então
        capturado pelo listener on_message neste mesmo canal.
        """
        uid = interaction.user.id
        self._upload_mode[uid] = time.time() + _UPLOAD_MODE_SECONDS
        emb = info_embed(
            "Modo upload ativado",
            "Solte o arquivo de vídeo NESTE canal privado nos próximos 5 minutos.",
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=emb, ephemeral=True)
            else:
                await interaction.response.send_message(embed=emb, ephemeral=True)
        except Exception:
            log.exception("Falha ao responder enable_upload_mode")

    # ------------------------------------------------------------------
    # Listener: importa vídeo dropado no canal privado durante o modo upload
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            if message.author.bot or not message.attachments:
                return
            if not _is_private_panel_channel(message.channel):
                return
            uid = message.author.id
            expiry = self._upload_mode.get(uid, 0.0)
            if time.time() >= expiry:
                self._upload_mode.pop(uid, None)
                return

            results: list[str] = []
            imported = 0
            for att in message.attachments:
                ok, msg, _vid = await self._import_video(message.author, att)
                imported += 1 if ok else 0
                results.append(f"{'✅' if ok else '⛔'} {msg}")

            summary = "\n".join(results)
            if imported:
                emb = success_embed(
                    f"{imported} vídeo(s) importado(s)", summary
                )
            else:
                emb = warn_embed("Nenhum vídeo válido", summary)
            try:
                await message.channel.send(embed=emb, delete_after=60)
            except Exception:
                log.exception("Falha ao avisar resultado do upload no painel")
        finally:
            # Sempre permite que comandos de prefixo (se houver) sejam processados.
            await self.bot.process_commands(message)

    # ------------------------------------------------------------------
    # Comandos de aplicação
    # ------------------------------------------------------------------
    @app_commands.command(
        name="enviar_video", description="Envia um arquivo de vídeo para suas sessões"
    )
    @app_commands.describe(arquivo="Arquivo de vídeo (máximo conforme configuração)")
    @not_suspended
    async def enviar_video(
        self, interaction: discord.Interaction, arquivo: discord.Attachment
    ) -> None:
        if not await check_cooldown(interaction, "enviar_video"):
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg, _vid = await self._import_video(interaction.user, arquivo)
        emb = success_embed("Upload concluído", msg) if ok else error_embed("Upload recusado", msg)
        await interaction.followup.send(embed=emb, ephemeral=True)

    @app_commands.command(
        name="meus_videos", description="Lista seus vídeos enviados (visível só para você)"
    )
    async def meus_videos(self, interaction: discord.Interaction) -> None:
        if not await check_cooldown(interaction, "meus_videos"):
            return
        uid = interaction.user.id
        with get_db() as con:
            ensure_user(con, uid)
            rows = con.execute(
                "SELECT * FROM videos WHERE user_id=? AND deleted=0 "
                "ORDER BY created_at DESC LIMIT ?",
                (uid, _LIST_LIMIT),
            ).fetchall()
            total = con.execute(
                "SELECT COUNT(*) AS c FROM videos WHERE user_id=? AND deleted=0",
                (uid,),
            ).fetchone()["c"]

        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "Sem vídeos",
                    "Você ainda não enviou nenhum vídeo. Use `/enviar_video` ou o "
                    "botão **📤 Enviar vídeo** no seu painel privado.",
                ),
                ephemeral=True,
            )
            return

        desc = f"**{total}** vídeo(s) ativo(s)"
        if total > _LIST_LIMIT:
            desc += f" — mostrando os {_LIST_LIMIT} mais recentes"
        emb = embed("Seus vídeos", desc)
        for r in rows:
            thumb_mark = " • 🖼️ com miniatura" if r["thumbnail_path"] else ""
            emb.add_field(
                name=f"#{r['id']} — {r['filename']}",
                value=(
                    f"Tamanho: {_fmt_bytes(r['size_bytes'])} • "
                    f"Enviado em {r['created_at']} UTC{thumb_mark}"
                ),
                inline=False,
            )

        # Anexa a primeira miniatura disponível (se existir) ao embed.
        thumb_file: discord.File | None = None
        first_thumb = next(
            (r["thumbnail_path"] for r in rows if r["thumbnail_path"]), None
        )
        if first_thumb and Path(first_thumb).exists():
            try:
                thumb_file = discord.File(first_thumb, filename="thumb.jpg")
                emb.set_thumbnail(url="attachment://thumb.jpg")
            except Exception:
                thumb_file = None

        await interaction.response.send_message(
            embed=emb, file=thumb_file, ephemeral=True
        )

    @app_commands.command(
        name="excluir_video",
        description="Exclui um vídeo (dono, admin ou proprietário)",
    )
    @app_commands.describe(video_id="ID numérico do vídeo (veja em /meus_videos)")
    async def excluir_video(
        self, interaction: discord.Interaction, video_id: int
    ) -> None:
        if not await check_cooldown(interaction, "excluir_video"):
            return
        row = await self.fetch_video(video_id)
        if row is None or row["deleted"]:
            await interaction.response.send_message(
                embed=error_embed(
                    "Vídeo não encontrado",
                    f"Não há vídeo ativo com id **#{video_id}**. "
                    "Confira o id em `/meus_videos`.",
                ),
                ephemeral=True,
            )
            return

        actor = interaction.user.id
        if not (is_admin(actor) or row["user_id"] == actor):
            await interaction.response.send_message(
                embed=error_embed(
                    "Acesso negado",
                    "Você só pode excluir seus próprios vídeos. "
                    "Admins e o proprietário podem excluir qualquer um.",
                ),
                ephemeral=True,
            )
            return

        emb = warn_embed(
            "Confirmar exclusão?",
            f"Vídeo **#{row['id']} — {row['filename']}** "
            f"({_fmt_bytes(row['size_bytes'])}).\n"
            "Esta ação remove o arquivo do disco e não pode ser desfeita.",
        )
        view = _DeleteConfirmView(self, video_id, actor)
        await interaction.response.send_message(embed=emb, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VideosCog(bot))
