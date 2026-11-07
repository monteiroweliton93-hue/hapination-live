
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
OWNER_DISCORD_ID = int(os.getenv("OWNER_DISCORD_ID", "0"))

DATA_DIR = Path("data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "hapination.db"

MAX_UPLOAD_MB = 25

SESSION_PRICES = {
    1: 100,
    6: 125,
    12: 150,
    18: 175,
    24: 200
}

DURATION_CHOICES = [
    app_commands.Choice(name="1 hora — R$ 1,00", value=1),
    app_commands.Choice(name="6 horas — R$ 1,25", value=6),
    app_commands.Choice(name="12 horas — R$ 1,50", value=12),
    app_commands.Choice(name="18 horas — R$ 1,75", value=18),
    app_commands.Choice(name="24 horas — R$ 2,00", value=24)
]

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# =========================
# BANCO DE DADOS
# =========================

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                credits_cents INTEGER NOT NULL DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME,
                ended_at DATETIME,
                duration_hours INTEGER NOT NULL DEFAULT 24
            )
        """)

        columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        ]

        if "expires_at" not in columns:
            con.execute(
                "ALTER TABLE sessions ADD COLUMN expires_at DATETIME"
            )

        if "duration_hours" not in columns:
            con.execute(
                "ALTER TABLE sessions ADD COLUMN duration_hours INTEGER DEFAULT 24"
            )

        con.execute("""
            CREATE TABLE IF NOT EXISTS credit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)


def ensure_user(user_id):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES (?)",
            (user_id,)
        )


def get_credits(user_id):
    ensure_user(user_id)

    with db() as con:
        row = con.execute(
            "SELECT credits_cents FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        return row["credits_cents"]


def is_owner(user_id):
    return OWNER_DISCORD_ID != 0 and user_id == OWNER_DISCORD_ID


def money(cents):
    return f"R$ {cents / 100:.2f}".replace(".", ",")


def session_price(hours):
    return SESSION_PRICES[hours]


# =========================
# BOT
# =========================

class HapinationBot(commands.Bot):

    async def setup_hook(self):
        init_db()
        synced = await self.tree.sync()
        print(f"{len(synced)} comandos sincronizados.")


intents = discord.Intents.default()

bot = HapinationBot(
    command_prefix="!",
    intents=intents
)


# =========================
# EXPIRAÇÃO DE SESSÕES
# =========================

@tasks.loop(minutes=1)
async def expire_sessions():
    with db() as con:
        con.execute("""
            UPDATE sessions
            SET status='expired',
                ended_at=CURRENT_TIMESTAMP
            WHERE status='running'
              AND expires_at IS NOT NULL
              AND expires_at <= CURRENT_TIMESTAMP
        """)


@bot.event
async def on_ready():
    if not expire_sessions.is_running():
        expire_sessions.start()

    print(f"Hapination Live conectado como {bot.user}")



# =========================
# PAINEL INTERATIVO
# =========================

class HapinationPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Enviar vídeo", emoji="🎥", style=discord.ButtonStyle.primary, row=0)
    async def enviar(self, interaction, button):
        await interaction.response.send_message(
            "Para enviar um vídeo, utilize o comando `/enviar_video`.",
            ephemeral=True
        )

    @discord.ui.button(label="Iniciar live", emoji="🔴", style=discord.ButtonStyle.success, row=0)
    async def iniciar(self, interaction, button):
        await interaction.response.send_message(
            "Para iniciar sua sessão simulada, utilize `/iniciar`.",
            ephemeral=True
        )

    @discord.ui.button(label="Saldo", emoji="💰", style=discord.ButtonStyle.primary, row=0)
    async def saldo(self, interaction, button):
        ensure_user(interaction.user.id)
        valor = get_credits(interaction.user.id)
        await interaction.response.send_message(
            f"💰 Seu saldo: **{money(valor)}**",
            ephemeral=True
        )

    @discord.ui.button(label="Encerrar live", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def encerrar(self, interaction, button):
        user_id = interaction.user.id

        with db() as con:
            sessao = con.execute(
                "SELECT id FROM sessions WHERE user_id=? AND status='running' ORDER BY id DESC LIMIT 1",
                (user_id,)
            ).fetchone()

            if not sessao:
                await interaction.response.send_message(
                    "Você não possui uma sessão ativa.",
                    ephemeral=True
                )
                return

            con.execute(
                "UPDATE sessions SET status='ended', ended_at=CURRENT_TIMESTAMP WHERE id=?",
                (sessao["id"],)
            )

        await interaction.response.send_message(
            "⏹️ Sessão encerrada.",
            ephemeral=True
        )


    @discord.ui.button(label="Meus vídeos", emoji="📁", style=discord.ButtonStyle.secondary, row=1)
    async def meus_videos(self, interaction, button):
        with db() as con:
            videos = con.execute(
                "SELECT id, filename FROM videos WHERE user_id=? ORDER BY id DESC LIMIT 10",
                (interaction.user.id,)
            ).fetchall()

        if not videos:
            mensagem = "Você ainda não enviou vídeos."
        else:
            mensagem = "\n".join(
                f"🎬 **#{v['id']}** — {v['filename']}" for v in videos
            )

        await interaction.response.send_message(
            mensagem, ephemeral=True
        )

    @discord.ui.button(label="Adicionar créditos", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def adicionar(self, interaction, button):
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "⛔ Apenas o proprietário pode adicionar créditos.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Utilize `/adicionar_creditos` para adicionar créditos.",
            ephemeral=True
        )

    @discord.ui.button(label="Renovar", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
    async def renovar(self, interaction, button):
        await interaction.response.send_message(
            "Utilize `/renovar` para renovar sua sessão.",
            ephemeral=True
        )

    @discord.ui.button(label="Horas", emoji="⏱️", style=discord.ButtonStyle.secondary, row=1)
    async def horas(self, interaction, button):
        linhas = [
            f"**{h} hora(s)** — {money(preco)}"
            for h, preco in SESSION_PRICES.items()
        ]

        await interaction.response.send_message(
            "⏱️ **Tabela de duração**\n\n" + "\n".join(linhas),
            ephemeral=True
        )


@bot.tree.command(
    name="painel",
    description="Abre o painel interativo do Hapination Live"
)
async def painel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬 HAPINATION LIVE",
        description=(
            "Bem-vindo ao painel de gerenciamento!\n\n"
            "Escolha uma das opções abaixo para continuar.\n\n"
            "⚠️ Sistema de demonstração com sessões simuladas."
        ),
        color=discord.Color.from_rgb(35, 25, 48)
    )

    await interaction.response.send_message(
        embed=embed,
        view=HapinationPanel(),
        ephemeral=True
    )


@bot.tree.command(
    name="horas",
    description="Mostra as durações e preços das sessões"
)
async def horas(interaction: discord.Interaction):

    linhas = [
        f"**{h} hora(s)** — {money(price)}"
        for h, price in SESSION_PRICES.items()
    ]

    embed = discord.Embed(
        title="⏱️ DURAÇÕES DISPONÍVEIS",
        description="\n".join(linhas),
        color=discord.Color.dark_purple()
    )

    embed.set_footer(
        text="Valores em créditos fictícios de demonstração."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================
# SALDO
# =========================

@bot.tree.command(
    name="saldo",
    description="Consulta seus créditos fictícios"
)
async def saldo(interaction: discord.Interaction):

    cents = get_credits(interaction.user.id)

    await interaction.response.send_message(
        f"💳 Seu saldo: **{money(cents)}** em créditos fictícios.",
        ephemeral=True
    )


# =========================
# ADICIONAR CRÉDITOS
# EXCLUSIVO DO DONO
# =========================

async def owner_check(interaction: discord.Interaction):
    return is_owner(interaction.user.id)


@bot.tree.command(
    name="adicionar_creditos",
    description="(Dono) Adiciona créditos fictícios a um usuário"
)
@app_commands.check(owner_check)
@app_commands.describe(
    usuario="Usuário que receberá os créditos",
    centavos="Valor em centavos (100 = R$ 1,00)"
)
async def adicionar_creditos(
    interaction: discord.Interaction,
    usuario: discord.Member,
    centavos: app_commands.Range[int, 1, 100000]
):

    ensure_user(usuario.id)

    with db() as con:
        con.execute(
            """
            UPDATE users
            SET credits_cents = credits_cents + ?
            WHERE user_id = ?
            """,
            (centavos, usuario.id)
        )

        con.execute(
            """
            INSERT INTO credit_logs(
                owner_id,
                user_id,
                amount_cents
            )
            VALUES (?, ?, ?)
            """,
            (
                interaction.user.id,
                usuario.id,
                centavos
            )
        )

    await interaction.response.send_message(
        f"✅ Foram adicionados **{money(centavos)}** "
        f"em créditos fictícios para {usuario.mention}.\n"
        f"👑 Operação registrada no histórico.",
        ephemeral=True
    )


# =========================
# ENVIAR VÍDEO
# =========================

@bot.tree.command(
    name="enviar_video",
    description="Envia um vídeo para sua biblioteca"
)
@app_commands.describe(
    arquivo="Vídeo próprio para teste"
)
async def enviar_video(
    interaction: discord.Interaction,
    arquivo: discord.Attachment
):

    if not (arquivo.content_type or "").startswith("video/"):
        await interaction.response.send_message(
            "Envie um arquivo de vídeo válido.",
            ephemeral=True
        )
        return

    if arquivo.size > MAX_UPLOAD_MB * 1024 * 1024:
        await interaction.response.send_message(
            f"O limite deste protótipo é {MAX_UPLOAD_MB} MB.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True,
        thinking=True
    )

    safe_name = (
        Path(arquivo.filename)
        .name
        .replace("/", "_")
        .replace("\\", "_")
    )

    stored_name = (
        f"{interaction.user.id}_"
        f"{discord.utils.utcnow().timestamp():.0f}_"
        f"{safe_name}"
    )

    target = UPLOAD_DIR / stored_name

    try:
        await arquivo.save(target)

    except Exception as error:
        print(f"Erro ao salvar vídeo: {error}")

        await interaction.followup.send(
            "Não consegui salvar o arquivo. Tente novamente.",
            ephemeral=True
        )
        return

    with db() as con:
        cur = con.execute(
            """
            INSERT INTO videos(user_id, filename, path)
            VALUES (?, ?, ?)
            """,
            (
                interaction.user.id,
                safe_name,
                str(target)
            )
        )

        video_id = cur.lastrowid

    await interaction.followup.send(
        f"✅ Vídeo cadastrado!\n"
        f"ID: **{video_id}**\n"
        f"Arquivo: `{safe_name}`",
        ephemeral=True
    )


# =========================
# MEUS VÍDEOS
# =========================

@bot.tree.command(
    name="meus_videos",
    description="Lista seus vídeos cadastrados"
)
async def meus_videos(interaction: discord.Interaction):

    with db() as con:
        rows = con.execute(
            """
            SELECT id, filename
            FROM videos
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (interaction.user.id,)
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "Você ainda não enviou vídeos.",
            ephemeral=True
        )
        return

    lines = [
        f"**ID {row['id']}** — {row['filename']}"
        for row in rows
    ]

    await interaction.response.send_message(
        "📂 **SUA BIBLIOTECA**\n\n" + "\n".join(lines),
        ephemeral=True
    )


# =========================
# INICIAR SESSÃO
# =========================

@bot.tree.command(
    name="iniciar",
    description="Inicia uma sessão simulada"
)
@app_commands.describe(
    video_id="ID mostrado em /meus_videos",
    duracao="Escolha a duração da sessão"
)
@app_commands.choices(duracao=DURATION_CHOICES)
async def iniciar(
    interaction: discord.Interaction,
    video_id: int,
    duracao: app_commands.Choice[int]
):

    user_id = interaction.user.id
    hours = duracao.value
    price = session_price(hours)

    ensure_user(user_id)

    with db() as con:

        con.execute(
            """
            UPDATE sessions
            SET status='expired',
                ended_at=CURRENT_TIMESTAMP
            WHERE user_id=?
              AND status='running'
              AND expires_at IS NOT NULL
              AND expires_at <= CURRENT_TIMESTAMP
            """,
            (user_id,)
        )

        existing = con.execute(
            """
            SELECT id
            FROM sessions
            WHERE user_id=?
              AND status='running'
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (user_id,)
        ).fetchone()

        if existing:
            await interaction.response.send_message(
                "Você já tem uma sessão ativa.",
                ephemeral=True
            )
            return

        video = con.execute(
            """
            SELECT id
            FROM videos
            WHERE id=? AND user_id=?
            """,
            (video_id, user_id)
        ).fetchone()

        if not video:
            await interaction.response.send_message(
                "Não encontrei esse vídeo na sua biblioteca.",
                ephemeral=True
            )
            return

        user = con.execute(
            """
            SELECT credits_cents
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if user["credits_cents"] < price:
            await interaction.response.send_message(
                f"Créditos insuficientes.\n"
                f"Necessário: **{money(price)}**\n"
                f"Disponível: **{money(user['credits_cents'])}**",
                ephemeral=True
            )
            return

        con.execute(
            """
            UPDATE users
            SET credits_cents=credits_cents-?
            WHERE user_id=?
            """,
            (price, user_id)
        )

        cur = con.execute(
            """
            INSERT INTO sessions(
                user_id,
                video_id,
                status,
                expires_at,
                duration_hours
            )
            VALUES (
                ?,
                ?,
                'running',
                datetime('now', ?),
                ?
            )
            """,
            (
                user_id,
                video_id,
                f"+{hours} hours",
                hours
            )
        )

        session_id = cur.lastrowid

    await interaction.response.send_message(
        f"🟢 **SESSÃO SIMULADA INICIADA**\n\n"
        f"Identificação: **#{session_id}**\n"
        f"Vídeo: **#{video_id}**\n"
        f"Duração: **{hours} hora(s)**\n"
        f"Créditos utilizados: **{money(price)}**\n\n"
        f"⚠️ Nenhuma transmissão externa foi iniciada.",
        ephemeral=True
    )


# =========================
# RENOVAR SESSÃO
# =========================

@bot.tree.command(
    name="renovar",
    description="Renova uma sessão vencida ou encerrada"
)
@app_commands.describe(
    duracao="Escolha a nova duração"
)
@app_commands.choices(duracao=DURATION_CHOICES)
async def renovar(
    interaction: discord.Interaction,
    duracao: app_commands.Choice[int]
):

    user_id = interaction.user.id
    hours = duracao.value
    price = session_price(hours)

    ensure_user(user_id)

    with db() as con:

        con.execute(
            """
            UPDATE sessions
            SET status='expired',
                ended_at=CURRENT_TIMESTAMP
            WHERE user_id=?
              AND status='running'
              AND expires_at IS NOT NULL
              AND expires_at <= CURRENT_TIMESTAMP
            """,
            (user_id,)
        )

        active = con.execute(
            """
            SELECT id
            FROM sessions
            WHERE user_id=?
              AND status='running'
            """,
            (user_id,)
        ).fetchone()

        if active:
            await interaction.response.send_message(
                "Você já tem uma sessão ativa. "
                "Ela não precisa ser renovada antes do vencimento.",
                ephemeral=True
            )
            return

        previous = con.execute(
            """
            SELECT id
            FROM sessions
            WHERE user_id=?
              AND status IN ('expired', 'ended')
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        if not previous:
            await interaction.response.send_message(
                "Não encontrei uma sessão anterior para renovar. "
                "Use `/iniciar` para abrir uma nova.",
                ephemeral=True
            )
            return

        user = con.execute(
            """
            SELECT credits_cents
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if user["credits_cents"] < price:
            await interaction.response.send_message(
                f"Créditos insuficientes.\n"
                f"Necessário: **{money(price)}**\n"
                f"Disponível: **{money(user['credits_cents'])}**",
                ephemeral=True
            )
            return

        con.execute(
            """
            UPDATE users
            SET credits_cents=credits_cents-?
            WHERE user_id=?
            """,
            (price, user_id)
        )

        con.execute(
            """
            UPDATE sessions
            SET status='running',
                started_at=CURRENT_TIMESTAMP,
                expires_at=datetime('now', ?),
                ended_at=NULL,
                duration_hours=?
            WHERE id=?
            """,
            (
                f"+{hours} hours",
                hours,
                previous["id"]
            )
        )

    await interaction.response.send_message(
        f"🔄 **SESSÃO RENOVADA**\n\n"
        f"Sessão: **#{previous['id']}**\n"
        f"Nova duração: **{hours} hora(s)**\n"
        f"Créditos utilizados: **{money(price)}**\n\n"
        f"Créditos fictícios de demonstração.",
        ephemeral=True
    )


# =========================
# ENCERRAR SESSÃO
# =========================

@bot.tree.command(
    name="encerrar",
    description="Encerra sua sessão simulada"
)
async def encerrar(interaction: discord.Interaction):

    with db() as con:
        row = con.execute(
            """
            SELECT id
            FROM sessions
            WHERE user_id=?
              AND status='running'
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (interaction.user.id,)
        ).fetchone()

        if not row:
            await interaction.response.send_message(
                "Você não tem sessão ativa.",
                ephemeral=True
            )
            return

        con.execute(
            """
            UPDATE sessions
            SET status='ended',
                ended_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (row["id"],)
        )

    await interaction.response.send_message(
        f"⏹️ Sessão **#{row['id']}** encerrada.",
        ephemeral=True
    )


# =========================
# TRATAMENTO DE ERROS
# =========================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    if isinstance(error, app_commands.CheckFailure):
        message = "⛔ Este comando é exclusivo do proprietário do Hapination Live."

    elif isinstance(error, app_commands.MissingPermissions):
        message = "Você não tem permissão para usar esse comando."

    else:
        print(f"Erro em comando: {repr(error)}")
        message = "Não foi possível executar o comando. Tente novamente."

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )


# =========================
# INICIALIZAÇÃO
# =========================

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Defina DISCORD_BOT_TOKEN no .env ou nas variáveis de ambiente."
        )

    if not OWNER_DISCORD_ID:
        raise SystemExit(
            "Defina OWNER_DISCORD_ID no arquivo .env."
        )

    bot.run(TOKEN)
