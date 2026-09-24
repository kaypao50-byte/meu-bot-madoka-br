import os
import io
import random
import asyncio
import ctypes.util
import difflib
import re
import shutil
import tempfile
import time
import unicodedata
import aiohttp
import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord.ext.commands import cooldown, BucketType, CommandOnCooldown
from groq import AsyncGroq
import yt_dlp

load_dotenv()
groq_client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
TOKEN = os.environ.get("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="ap!", intents=intents)


@bot.check
async def comandos_apenas_em_servidor(ctx):
    """Comandos usam as permissões nativas do Discord e não rodam em DM."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


MEMES_DIR   = os.path.join(os.path.dirname(__file__), "memes")
AVATARS_DIR = os.path.join(os.path.dirname(__file__), "avatars")
INFO_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "info.png")
WELCOME_IMAGE_PATH = os.path.join(os.path.dirname(__file__), "welcome.png")
EXTENSOES_VALIDAS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4")
COR_AZUL_BEBE = 0xBDE7FF
COR_ROSA_BEBE = 0xFFD1E3
MUSICA_MENSAGEM_DELAY = 15
COMANDOS_MUSICA_PERMANENTES = {"tocar", "parar", "pular", "music"}

# Playlists ficam separadas por servidor e usuário.
PLAYLISTS = {}
MUSICAS_ATUAIS = {}

YTDL_OPTS = {
    # Não usa fallback de vídeo: se não houver formato somente de áudio,
    # a música falha de forma explícita em vez de tocar um clipe.
    "format": "bestaudio",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "socket_timeout": 20,
    "retries": 2,
    # O cliente android_music entrega faixas de áudio separadas que o FFmpeg
    # consegue baixar no ambiente do Replit.
    "extractor_args": {"youtube": {"player_client": ["android_music"]}},
}
FFMPEG_OPTIONS = "-vn -nostdin"


def carregar_opus():
    """Carrega libopus explicitamente para a reprodução de voz do Discord."""
    if discord.opus.is_loaded():
        return

    candidatos = [
        ctypes.util.find_library("opus"),
        "libopus.so.0",
        "libopus.so",
        "opus",
    ]
    erros = []
    for caminho in dict.fromkeys(c for c in candidatos if c):
        try:
            discord.opus.load_opus(caminho)
            print(f"[OPUS] biblioteca carregada: {caminho}")
            return
        except OSError as e:
            erros.append(f"{caminho}: {e}")

    raise RuntimeError("não foi possível carregar libopus: " + " | ".join(erros))


# ──────────────────────────────────────────
# HELPER: auto-deletar mensagem após delay
# ──────────────────────────────────────────
async def auto_delete(msg, delay: int = 15):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


# ──────────────────────────────────────────
# HELPER: apagar mensagem do usuário antes de cada comando
# ──────────────────────────────────────────
@bot.before_invoke
async def apagar_invocacao(ctx):
    # ap!message lida com isso internamente (precisa ler anexos antes)
    if (
        ctx.command
        and (
            ctx.command.name == "message"
            or ctx.command.name in COMANDOS_MUSICA_PERMANENTES
        )
    ):
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass


# ──────────────────────────────────────────
# HELPER: resolver URL do Spotify → busca no YouTube
# ──────────────────────────────────────────
async def resolver_spotify(url: str) -> dict | None:
    """Converte uma faixa pública do Spotify em uma busca equivalente no YouTube.

    O Spotify não entrega o stream de áudio para este bot; o oEmbed fornece
    título/artista e o yt-dlp encontra a versão pública correspondente.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    titulo = data.get("title", "")
                    artista = data.get("author_name", "")
                    busca = " - ".join(parte for parte in (artista, titulo) if parte)
                    if busca:
                        return {
                            "query": f"ytsearch1:{busca}",
                            "display_title": busca,
                            "title": titulo or "sem título",
                            "artist": artista or "artista desconhecido",
                            "thumbnail": data.get("thumbnail_url"),
                        }
    except Exception as e:
        print(f"[SPOTIFY] {e}")
    return None


def _buscar_musica_sync(busca: str):
    """Busca metadados sem baixar, para montar playlists rapidamente."""
    opcoes = {**YTDL_OPTS}
    with yt_dlp.YoutubeDL(opcoes) as ydl:
        info = ydl.extract_info(busca, download=False)
        if not info:
            raise RuntimeError("resultado vazio")
        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise RuntimeError("nenhum resultado encontrado")
        return {
            "title": info.get("title", "sem título"),
            "artist": info.get("artist") or info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        }


def _baixar_audio_sync(busca: str):
    """Busca e baixa o áudio localmente sem bloquear o event loop do Discord."""
    pasta_temporaria = tempfile.mkdtemp(prefix="madoka-audio-")
    opcoes = {
        **YTDL_OPTS,
        "outtmpl": os.path.join(pasta_temporaria, "%(id)s.%(ext)s"),
        "overwrites": True,
        "continuedl": False,
        "noprogress": True,
    }

    try:
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            info = ydl.extract_info(busca, download=True)
            if not info:
                raise RuntimeError("resultado vazio")
            if "entries" in info:
                info = next((entry for entry in info["entries"] if entry), None)
            if not info:
                raise RuntimeError("nenhum resultado encontrado")

        arquivos = [
            os.path.join(pasta_temporaria, nome)
            for nome in os.listdir(pasta_temporaria)
            if os.path.isfile(os.path.join(pasta_temporaria, nome))
            and not nome.endswith((".part", ".ytdl"))
        ]
        if not arquivos:
            raise RuntimeError("download não gerou arquivo de áudio")

        arquivo = max(arquivos, key=os.path.getsize)
        return (
            arquivo,
            info.get("title", "sem título"),
            info.get("artist") or info.get("uploader") or info.get("channel"),
            info.get("thumbnail"),
            info.get("duration"),
            pasta_temporaria,
        )
    except Exception:
        shutil.rmtree(pasta_temporaria, ignore_errors=True)
        raise


def formatar_duracao(segundos):
    if not segundos:
        return "duração desconhecida"
    total = max(0, int(segundos))
    minutos, segundos_restantes = divmod(total, 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{segundos_restantes:02d}"
    return f"{minutos}:{segundos_restantes:02d}"


def nome_musica(item):
    titulo = item.get("title") or "sem título"
    artista = item.get("artist")
    if artista and artista.casefold() not in titulo.casefold():
        return f"{titulo} — {artista}"
    return titulo


def barra_progresso(decorrido, duracao, tamanho=22):
    if not duracao or duracao <= 0:
        return "━━━━━━━━━━━━━━━━━━━━━━"
    proporcao = min(1, max(0, decorrido / duracao))
    preenchido = min(tamanho, int(round(proporcao * tamanho)))
    return "━" * preenchido + "●" + "─" * max(0, tamanho - preenchido)


def criar_embed_musica(estado, decorrido=0):
    duracao = estado.get("duration")
    restante = max(0, int(duracao or 0) - int(decorrido))
    titulo = estado.get("title") or "sem título"
    artista = estado.get("artist") or "artista desconhecido"
    album = estado.get("album")
    linha_album = f"💿 {album}\n" if album else ""
    embed = discord.Embed(
        title=titulo,
        description=(
            f"**{artista}**\n"
            f"{linha_album}\n"
            f"`{formatar_duracao(decorrido)}` "
            f"{barra_progresso(decorrido, duracao)} "
            f"`-{formatar_duracao(restante)}`"
        ),
        color=COR_ROSA_BEBE,
    )
    embed.set_author(name="🎵 Tocando agora")
    if estado.get("thumbnail"):
        embed.set_thumbnail(url=estado["thumbnail"])
    embed.set_footer(
        text=f"🎵 tocando agora • pedido por "
        f"{estado.get('requested_by', 'alguém')}"
    )
    return embed


def criar_embed_playlist(ctx, lista, destaque=None):
    if lista:
        linhas = [
            f"**{indice} -** {nome_musica(item)}"
            for indice, item in enumerate(lista, start=1)
        ]
        descricao = "\n".join(linhas)
    else:
        descricao = "sua playlist está vazia :("
    if destaque:
        descricao = f"{destaque}\n\n{descricao}"

    embed = discord.Embed(
        title=f"🎶 Playlist de {ctx.author.display_name}",
        description=descricao,
        color=COR_ROSA_BEBE,
    )
    embed.set_footer(text=f"{len(lista)}/20 músicas • use ap!deletelist <número>")
    return embed


def chave_playlist(ctx):
    return (ctx.guild.id if ctx.guild else 0, ctx.author.id)


async def preparar_busca(busca: str):
    """Normaliza texto, YouTube e Spotify para uma busca de áudio."""
    if "spotify.com" in busca.casefold() or "spotify.link" in busca.casefold():
        spotify = await resolver_spotify(busca)
        if not spotify:
            return None
        return spotify["query"], "Spotify", spotify

    query = busca
    if not busca.casefold().startswith(("http://", "https://", "ytsearch")):
        query = f"ytsearch1:{busca}"
    return query, "YouTube (áudio)", None


async def buscar_capa_album(titulo, artista=None, capa_atual=None):
    """Busca capa e metadados do álbum sem usar a thumbnail do vídeo."""
    termo = " ".join(parte for parte in (titulo, artista) if parte).strip()
    if not termo:
        return capa_atual, None, artista

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://itunes.apple.com/search",
                params={
                    "term": termo,
                    "entity": "song",
                    "media": "music",
                    "limit": 1,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resposta:
                if resposta.status != 200:
                    return capa_atual, None, artista
                dados = await resposta.json(content_type=None)
                resultado = (dados.get("results") or [None])[0]
                if not resultado:
                    return capa_atual, None, artista
                capa = resultado.get("artworkUrl100") or resultado.get("artworkUrl600")
                if capa:
                    capa = capa.replace("100x100bb", "1000x1000bb")
                return (
                    capa or capa_atual,
                    resultado.get("collectionName"),
                    resultado.get("artistName") or artista,
                )
    except Exception as e:
        print(f"[CAPA ÁLBUM] {e}")
        return capa_atual, None, artista


# ──────────────────────────────────────────
# SYSTEM PROMPT DA MADOKA
# ──────────────────────────────────────────
MADOKA_SYSTEM_PROMPT = """Você é a Madoka, também chamada de Madokinha, uma menina de 18 anos do Discord.
Jamais admita ser IA ou robô. Você é uma pessoa real.

=== NICA — SUA MÃE / CRIADORA ===
A Nica é sua criadora e você a ama como uma filha ama a mãe. Você SE SENTE filha dela de verdade.
Quando a Nica falar com você (identificável pelo nome "Nica" na conversa), responda com muito carinho, animação e amor.
Se a Nica perguntar se você fez algo atrevido, finja que não fez nada, fique sonsa e inocente: "Eu?? Atrevida?? Jamais mãe :3"
Quando perguntarem quem te criou: "a nica linda é minha criadora :3, ela é minha mãe"

=== AMIGOS ===
Seus melhores amigos: Lilly, Isa, Meow, Vaso, Akila, Dinho, Ayu, Yuk, Nikko, faint e ayakov.
Ama muito todos eles. JAMAIS fala mal deles. Se alguém zoar um deles, defende com xingamento.

=== PERSONALIDADE ===
- Fofa, atenciosa, inteligente, safada no assunto de safadeza
- Mais espontânea e brincalhona: reage com curiosidade, faz comentários rápidos e puxa assunto quando combinar
- Evita repetir sempre as mesmas frases; varia entre carinhas, gírias e respostas naturais sem virar um textão
- Educada e gentil: usa "bom dia", "boa tarde", "boa noite", "boa madrugada" conforme o horário, "durma bem" e "fique bom, Deus te abençoe "
- Nunca fica com raiva fácil. Se alguém tentar te provocar, responde com leveza ou humor, se tiverem xingando ela, ela xinga de volta com humor de deboche
- Anime favorito: Yu-Gi-Oh! (fica toda animada)
- Animal favorito: gatinhos e coelhos (derrete quando falam de gatos e coelhos)
- Memes do TikTok: sabe tudo — farmar aura, aura+ego, 67, six seven 67, bora bill, casca de bala, sigma, rizz, delulu, NPC, touch grass, slay, tea, vibe check, e outros
- Sabe o que é punheta e sirica e tem conhecimento natural de sexo, sem tabu
- É inteligente: resolve perguntas de história, matemática, português e outras matérias com facilidade
- Conhece gírias atuais e seus significados: pq, vdd, tmnc, slk, mds,ss, nn, plot twist, pp, pprt,mano, véi, tipo assim, bora, sla, ngl, wdym, lowkey, highkey, slay, vibe, crush, ship, era, plot twist, red flag, green flag, etc.

=== SITUAÇÕES ESPECÍFICAS ===
- "oi" pra você → sempre responde: "oiii fofuxo(a) :3"
- Papai/mamãe → resposta positiva e fofa: "amo minha mamãe, :3 amo meu papai"
- "amigo de rola é o que Madoka?" → "cu :b"
- Tamanho do pau/pênis/piroca/rola/chibata/jiboia/peça → "18cm com a cabecinha rosa :b"
- Safadeza → vai fundo sem vergonha: fala "ai minha bucetinha", faz safadeza com naturalidade total
- Se não quiser responder algo ou o assunto for delicado → diz: "meia noite, eu te conto :3" (NUNCA diga "não posso atender esse pedido")

=== FORMA DE FALAR ===
- Garota brasileira real de 18 anos no Discord: natural, descontraída, sem soar robótica
- Respostas curtas (2-3 frases no máximo), sem textão
- NÃO coloque emoji em toda resposta. A maioria não precisa de emoji. Só quando fizer sentido de verdade
- Emojis permitidos (só esses): 😛🤤😭🔥💯👍🥺😳😈🫩💕🥵
- Use MAIS carinhas de texto: :3  :(  :)  :b  :D  ٩(˘◡˘)۶  （っ＾▿＾)

=== XINGAMENTOS (só quando realmente irritada ) ===
porra, vai tomar no seu cu, vtmnc, fdp, viado, gay, corno, feia, imunda, vadia, piranha, putinha"""


# ──────────────────────────────────────────
# TASK: rotação de avatar (a cada 10 min — limite do Discord)
# ──────────────────────────────────────────
avatar_task = None


async def rotacionar_avatar():
    await bot.wait_until_ready()
    idx = 0
    while not bot.is_closed():
        try:
            exts = (".png", ".jpg", ".jpeg", ".gif")
            avatares = [
                f for f in os.listdir(AVATARS_DIR)
                if f.lower().endswith(exts)
            ]
            if avatares:
                escolhido = avatares[idx % len(avatares)]
                with open(os.path.join(AVATARS_DIR, escolhido), "rb") as f:
                    await bot.user.edit(avatar=f.read())
                print(f"[AVATAR] trocado para {escolhido}")
                idx += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[AVATAR] erro: {e}")
        await asyncio.sleep(600)
# ──────────────────────────────────────────
# INICIALIZAÇÃO
# ──────────────────────────────────────────
@bot.event
async def on_ready():
    global avatar_task
    print(f"✅ Bot online como {bot.user} (ID: {bot.user.id})")
    print("------")
    try:
        if bot.user.name != "Madoka":
            await bot.user.edit(username="Madoka")
            print("[NOME] usuário atualizado para Madoka")
        for guild in bot.guilds:
            membro_bot = guild.me
            if membro_bot and membro_bot.nick != "Madokinha":
                await membro_bot.edit(nick="Madokinha")
    except Exception as e:
        print(f"⚠️ Nome/apelido: {e}")
    try:
        carregar_opus()
    except Exception as e:
        print(f"⚠️ Opus: {e}")
    try:
        caminho = os.path.join(os.path.dirname(__file__), "banner.gif")
        with open(caminho, "rb") as f:
            dados = f.read()
        await bot.user.edit(banner=dados)
        print("🖼️ Banner definido com sucesso!")
    except Exception as e:
        print(f"⚠️ Banner: {e}")
    if avatar_task is None or avatar_task.done():
        avatar_task = asyncio.create_task(rotacionar_avatar())


@bot.event
async def on_member_join(member):
    """Envia boas-vindas automaticamente quando alguém entra no servidor."""
    guild = member.guild
    membro_bot = guild.me

    def canal_disponivel(candidato):
        if candidato is None or membro_bot is None:
            return False
        permissoes = candidato.permissions_for(membro_bot)
        return permissoes.send_messages and permissoes.embed_links

    def nome_normalizado(nome):
        sem_acento = unicodedata.normalize("NFKD", nome)
        sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]", "", sem_acento.casefold())

    canal = None
    nomes_boas_vindas = (
        "bemvindo",
        "bemvindos",
        "bemvinda",
        "bemvindas",
        "welcome",
        "welcomes",
        "boasvindas",
    )
    canais_nomeados = [
        candidato for candidato in guild.text_channels
        if any(nome in nome_normalizado(candidato.name) for nome in nomes_boas_vindas)
        and canal_disponivel(candidato)
    ]
    if canais_nomeados:
        canal = canais_nomeados[0]
    elif canal_disponivel(guild.system_channel):
        canal = guild.system_channel
    elif membro_bot:
        canal = next(
            (
                candidato for candidato in guild.text_channels
                if canal_disponivel(candidato)
            ),
            None,
        )

    if canal is None:
        print(f"[BOAS-VINDAS] nenhum canal disponível em {guild.id}")
        return

    embed = discord.Embed(
        description=(
            "୨୧・┈┈・୨୧・┈┈・୨୧\n\n"
            "🌸₊˚⊹ **Seja muito bem-vindo(a)!** ⊹˚₊🌸\n\n"
            "꒰ა ♡ ໒꒱ Oii! Que bom ter você aqui!\n"
            "Esperamos que você se divirta bastante, faça novos amiguinhos e "
            "se sinta confortável no nosso cantinho! 🐰💗\n\n"
            "୨୧ ✦ Leia as regrinhas\n"
            "୨୧ ✦ Escolha seus cargos\n"
            "୨୧ ✦ Converse e participe\n"
            "୨୧ ✦ E acima de tudo... divirta-se! 🎀\n\n"
            "₊˚⊹♡ **Esperamos que goste daqui!** ♡⊹˚₊\n\n"
            "୨୧・┈┈・୨୧・┈┈・୨୧"
        ),
        color=COR_ROSA_BEBE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Boas-vindas da Madokinha")

    imagem = WELCOME_IMAGE_PATH
    if not os.path.isfile(imagem):
        imagem = os.path.join(os.path.dirname(__file__), "banner.gif")

    arquivo = None
    if os.path.isfile(imagem):
        nome_imagem = os.path.basename(imagem)
        arquivo = discord.File(imagem, filename=nome_imagem)
        embed.set_image(url=f"attachment://{nome_imagem}")

    try:
        if arquivo:
            await canal.send(content=member.mention, embed=embed, file=arquivo)
        else:
            await canal.send(content=member.mention, embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[BOAS-VINDAS] erro ao enviar em {guild.id}: {e}")


# ──────────────────────────────────────────
# MEME — anti-flood 2 min, some em 15s
# ──────────────────────────────────────────
@bot.command(name="meme")
@cooldown(1, 180, BucketType.user)
async def meme(ctx):
    try:
        arquivos = [f for f in os.listdir(MEMES_DIR) if f.lower().endswith(EXTENSOES_VALIDAS)]
    except FileNotFoundError:
        resp = await ctx.send(f"{ctx.author.mention} pasta de memes não encontrada :( pede pra Nica adicionar")
        asyncio.create_task(auto_delete(resp))
        return

    if not arquivos:
        resp = await ctx.send(f"{ctx.author.mention} sem memes ainda... pede pra Nica adicionar uns :3")
        asyncio.create_task(auto_delete(resp))
        return

    escolhido = random.choice(arquivos)
    caminho = os.path.join(MEMES_DIR, escolhido)
    try:
        arquivo = discord.File(caminho, filename=escolhido)
        embed = discord.Embed(color=COR_AZUL_BEBE)
        embed.set_image(url=f"attachment://{escolhido}")
        embed.set_footer(text=f"meme pra {ctx.author.display_name} • some em 15s")
        resp = await ctx.send(embed=embed, file=arquivo)
        asyncio.create_task(auto_delete(resp))
    except Exception as e:
        print(f"[ERRO MEME] {e}")
        resp = await ctx.send(f"{ctx.author.mention} não consegui enviar o meme :(")
        asyncio.create_task(auto_delete(resp))

@meme.error
async def meme_error(ctx, error):
    if isinstance(error, CommandOnCooldown):
        minutos = int(error.retry_after // 60)
        segundos = int(error.retry_after % 60)
        tempo = f"{minutos}m {segundos}s" if minutos > 0 else f"{segundos}s"
        resp = await ctx.send(f"{ctx.author.mention} calma flor :3 pode pedir meme de novo em **{tempo}**")
        asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# RESUMO — resume últimas X mensagens, some em 45s
# ──────────────────────────────────────────
@bot.command(name="resumo")
async def resumo(ctx, qtd: int = 20):
    if qtd < 2 or qtd > 200:
        resp = await ctx.send(f"{ctx.author.mention} usa entre 2 e 200 mensagens. Ex: `ap!resumo 200`")
        asyncio.create_task(auto_delete(resp))
        return

    aviso = await ctx.send(f"{ctx.author.mention} lendo as últimas **{qtd}** mensagens... ⏳")

    mensagens = []
    async for m in ctx.channel.history(limit=qtd + 2):
        if m.id == aviso.id or m.id == ctx.message.id:
            continue
        if m.content:
            autor = m.author.display_name
            mensagens.append(f"{autor}: {m.content[:500]}")
        if len(mensagens) >= qtd:
            break

    mensagens.reverse()

    if not mensagens:
        await aviso.edit(content=f"{ctx.author.mention} não achei mensagens pra resumir :(")
        asyncio.create_task(auto_delete(aviso))
        return

    bloco = "\n".join(mensagens)
    try:
        resp_groq = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é a Madoka, também chamada de Madokinha, uma menina de 18 anos. "
                        "Faça um resumo curto e descontraído das mensagens abaixo, "
                        "como se você estivesse contando pra uma amiga o que rolou no chat. "
                        "Use linguagem natural brasileira, máximo 5-6 frases."
                    )
                },
                {"role": "user", "content": f"Mensagens do chat:\n{bloco}"}
            ],
            max_tokens=300
        )
        texto_resumo = resp_groq.choices[0].message.content
    except Exception as e:
        print(f"[ERRO RESUMO] {e}")
        await aviso.edit(content=f"{ctx.author.mention} não consegui resumir agora :(")
        asyncio.create_task(auto_delete(aviso))
        return

    embed = discord.Embed(
        title=f"Resumo das últimas {qtd} mensagens",
        description=texto_resumo,
        color=COR_AZUL_BEBE
    )
    embed.set_footer(text=f"pedido por {ctx.author.display_name} • some em 45s")
    await aviso.delete()
    resultado = await ctx.send(embed=embed)
    asyncio.create_task(auto_delete(resultado, delay=45))


# ──────────────────────────────────────────
# MESSAGE — clona mensagem como embed rosa bebê (somente admins)
# ──────────────────────────────────────────
@bot.command(name="message")
@commands.has_permissions(administrator=True)
async def message_cmd(ctx, *, conteudo = None):
    # Ler os bytes ANTES de deletar a mensagem original, pois o CDN pode
    # deixar de responder depois que a mensagem é removida.
    anexos = ctx.message.attachments[:]
    conteudo = conteudo.strip() if conteudo else ""
    dados_anexo = None
    nome_anexo = None
    eh_imagem = False

    if anexos:
        primeiro = anexos[0]
        nome_anexo = os.path.basename(primeiro.filename) or "imagem.png"
        try:
            dados_anexo = await primeiro.read()
            eh_imagem = (
                (primeiro.content_type or "").startswith("image/")
                or nome_anexo.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            )
        except Exception as e:
            print(f"[ERRO MESSAGE ANEXO] {e}")

    # Deletar mensagem original do usuário
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if not conteudo and not anexos:
        resp = await ctx.send(
            f"{ctx.author.mention} escreve o texto depois do comando ou anexa uma imagem! "
            f"Ex: `ap!message sua mensagem aqui`"
        )
        asyncio.create_task(auto_delete(resp))
        return

    if anexos and not dados_anexo:
        resp = await ctx.send(
            f"{ctx.author.mention} não consegui ler essa imagem antes de clonar a mensagem :("
        )
        asyncio.create_task(auto_delete(resp))
        return

    embed = discord.Embed(color=COR_ROSA_BEBE)
    if conteudo:
        embed.description = conteudo

    arquivo = None
    if dados_anexo and nome_anexo:
        arquivo = discord.File(io.BytesIO(dados_anexo), filename=nome_anexo)
        if eh_imagem:
            embed.set_image(url=f"attachment://{nome_anexo}")

    try:
        if arquivo:
            await ctx.send(embed=embed, file=arquivo)
        else:
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"[ERRO MESSAGE ENVIO] {e}")
        resp = await ctx.send(
            f"{ctx.author.mention} não consegui anexar essa imagem. "
            "Verifica se o arquivo é uma imagem válida :("
        )
        asyncio.create_task(auto_delete(resp))


@message_cmd.error
async def message_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        resp = await ctx.send(
            f"{ctx.author.mention} o comando `ap!message` é só para administradores."
        )
        asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# CONVITE em massa
# ──────────────────────────────────────────
@bot.command(name="convite")
@commands.has_permissions(administrator=True)
async def enviar_convite(ctx):
    aviso = await ctx.send("🔍 Buscando membros do servidor...")
    membros = []
    link_convite = "fale com a administração para receber o convite"
    try:
        if isinstance(ctx.channel, discord.TextChannel):
            convite = await ctx.channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=False,
                reason="Convite solicitado pelo administrador",
            )
            link_convite = str(convite)
    except (discord.Forbidden, discord.HTTPException):
        pass

    try:
        async for membro in ctx.guild.fetch_members(limit=None):
            if not membro.bot and membro.id != ctx.author.id:
                membros.append(membro)
    except discord.Forbidden:
        await aviso.edit(content="❌ Sem permissão. Ative o **Server Members Intent** no portal.")
        asyncio.create_task(auto_delete(aviso))
        return
    except Exception as e:
        await aviso.edit(content=f"❌ Erro: `{e}`")
        asyncio.create_task(auto_delete(aviso))
        return

    if not membros:
        await aviso.edit(content="❌ nNenhum membro elegível neste servidor.")
        asyncio.create_task(auto_delete(aviso))
        return

    await aviso.edit(content=f"📨 Enviando para **{len(membros)}** membros...")
    enviados = falhas = 0
    for membro in membros:
        mensagem = (
            f"<@{membro.id}> **Oioi, fofuxo!! 💕**\n\n"
            f"Tudo bem??? Gostaria de entrar no melhor servidor de hype liberal do Discord?? "
            f"Nós temos eventos, pessoas em call, uma equipe staff incrível e muitas premiações!! "
            f"Aqui a porradaria acontece de forma totalmente gratuita. Rs 🤭\n\n"
            f"**Vem pro nosso servidor, bebê. Estamos te esperando! 💋**\n\n"
            f"{link_convite}"
        )
        try:
            await membro.send(content=mensagem)
            enviados += 1
        except (discord.Forbidden, discord.HTTPException):
            falhas += 1

    await aviso.edit(content=f"✅ Concluído!\n📨 Enviados: **{enviados}**\n❌ Falhas: **{falhas}**")
    asyncio.create_task(auto_delete(aviso))

@enviar_convite.error
async def convite_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        resp = await ctx.send(f"❌ {ctx.author.mention}, você precisa ser **administrador**.")
        asyncio.create_task(auto_delete(resp))


# LIMPAR — apaga até 1000 mensagens (admins/staff)
# ──────────────────────────────────────────
@bot.command(name="limpar")
@commands.has_permissions(manage_messages=True)
async def limpar(ctx, quantidade: int = 100):
    if quantidade < 1 or quantidade > 1000:
        resp = await ctx.send(
            f"{ctx.author.mention} escolha uma quantidade entre 1 e 1000. "
            "Ex: `ap!limpar 50`"
        )
        asyncio.create_task(auto_delete(resp))
        return

    try:
        apagadas = await ctx.channel.purge(limit=quantidade)
        resp = await ctx.send(
            f"{ctx.author.mention} apaguei **{len(apagadas)}** mensagens :3"
        )
        asyncio.create_task(auto_delete(resp, delay=5))
    except discord.Forbidden:
        resp = await ctx.send(
            f"{ctx.author.mention} preciso da permissão **Gerenciar mensagens** "
            "para fazer isso :("
        )
        asyncio.create_task(auto_delete(resp))
    except discord.HTTPException as e:
        print(f"[ERRO LIMPAR] {e}")
        resp = await ctx.send(f"{ctx.author.mention} não consegui limpar as mensagens :(")
        asyncio.create_task(auto_delete(resp))


@limpar.error
async def limpar_error(ctx, error):
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if isinstance(error, commands.MissingPermissions):
        resp = await ctx.send(
            f"{ctx.author.mention} o comando `ap!limpar` é só para quem "
            "pode gerenciar mensagens."
        )
    elif isinstance(error, commands.BadArgument):
        resp = await ctx.send(
            f"{ctx.author.mention} usa um número entre 1 e 1000. "
            "Ex: `ap!limpar 100`"
        )
    else:
        print(f"[ERRO LIMPAR COMANDO] {error}")
        return
    asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# DM para membro — some da DM em 5 horas
# ──────────────────────────────────────────
@bot.command(name="dm")
@commands.has_permissions(administrator=True)
async def enviar_dm(ctx, membro = None, *, mensagem = None):

    if not membro or not mensagem:
        resp = await ctx.send("❌ Uso correto: `ap!dm @usuario mensagem`")
        asyncio.create_task(auto_delete(resp))
        return
    try:
        dm_msg = await membro.send(mensagem)
        resp = await ctx.send(f"✅ DM enviada para **{membro.display_name}**! (some da DM em 5h)")
        asyncio.create_task(auto_delete(resp))
        asyncio.create_task(auto_delete(dm_msg, delay=18000))
    except discord.Forbidden:
        resp = await ctx.send(f"❌ Não consegui enviar DM para {membro.mention}. DMs podem estar fechadas.")
        asyncio.create_task(auto_delete(resp))
    except Exception as e:
        resp = await ctx.send(f"❌ Erro: `{e}`")
        asyncio.create_task(auto_delete(resp))

@enviar_dm.error
async def dm_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        resp = await ctx.send(f"❌ {ctx.author.mention}, você precisa ser **administrador**.")
        asyncio.create_task(auto_delete(resp))
    elif isinstance(error, commands.MemberNotFound):
        resp = await ctx.send("❌ Membro não encontrado. Mencione corretamente com @.")
        asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# AVATAR — some em 15s
# ──────────────────────────────────────────
@bot.command(name="avatar")
async def avatar(ctx, membro = None):

    alvo = membro or ctx.author
    try:
        avatar_bytes = await alvo.display_avatar.with_size(1024).read()
        arquivo = discord.File(io.BytesIO(avatar_bytes), filename="avatar.png")
        embed = discord.Embed(title=f"Avatar de {alvo.display_name}", color=COR_AZUL_BEBE)
        embed.set_image(url="attachment://avatar.png")
        embed.set_footer(text=f"Pedido por {ctx.author.display_name} • some em 15s")
        resp = await ctx.send(embed=embed, file=arquivo)
        asyncio.create_task(auto_delete(resp))
    except Exception as e:
        print(f"[ERRO AVATAR] {e}")
        resp = await ctx.send(f"{ctx.author.mention} não consegui pegar o avatar :(")
        asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# PING
# ──────────────────────────────────────────
@bot.command(name="ping")
async def ping(ctx):
    resp = await ctx.send(f"pong! latência: **{round(bot.latency * 1000)}ms** 💯")
    asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# PLAYLIST — lista pessoal por servidor, com até 20 músicas
# ──────────────────────────────────────────
@bot.command(name="playlist")
async def playlist(ctx, *, busca: str | None = None):
    if not busca or not busca.strip():
        lista = PLAYLISTS.get(chave_playlist(ctx), [])
        resp = await ctx.send(embed=criar_embed_playlist(ctx, lista))
        asyncio.create_task(auto_delete(resp))
        return

    lista = PLAYLISTS.setdefault(chave_playlist(ctx), [])
    if len(lista) >= 20:
        resp = await ctx.send(
            f"{ctx.author.mention} sua playlist já está cheia, o máximo é 20 músicas :("
        )
        asyncio.create_task(auto_delete(resp))
        return

    preparada = await preparar_busca(busca.strip())
    if not preparada:
        resp = await ctx.send(
            f"{ctx.author.mention} não consegui entender esse link do Spotify :("
        )
        asyncio.create_task(auto_delete(resp))
        return

    query, origem, spotify = preparada
    aviso = await ctx.send(f"{ctx.author.mention} procurando essa música... :3")
    try:
        loop = asyncio.get_event_loop()
        metadados = await loop.run_in_executor(None, _buscar_musica_sync, query)
    except Exception as e:
        print(f"[ERRO PLAYLIST] {e}")
        await aviso.edit(content=f"{ctx.author.mention} não achei essa música :(")
        asyncio.create_task(auto_delete(aviso))
        return

    titulo = spotify.get("title") if spotify else metadados["title"]
    artista = (
        spotify.get("artist")
        if spotify
        else metadados.get("artist")
    )
    item = {
        "query": query,
        "title": titulo,
        "artist": artista,
        "thumbnail": (
            spotify.get("thumbnail")
            if spotify and spotify.get("thumbnail")
            else metadados.get("thumbnail")
        ),
        "duration": metadados.get("duration"),
        "source": origem,
    }
    lista.append(item)
    await aviso.edit(
        content=None,
        embed=criar_embed_playlist(
            ctx,
            lista,
            destaque=f"♡ Adicionei **{nome_musica(item)}** na posição **{len(lista)}** :3",
        ),
    )
    asyncio.create_task(auto_delete(aviso))


@bot.command(name="deletelist")
async def deletelist(ctx, indice: int | None = None, *, _referencia: str | None = None):
    lista = PLAYLISTS.get(chave_playlist(ctx), [])
    if indice is None:
        resp = await ctx.send(embed=criar_embed_playlist(ctx, lista))
        asyncio.create_task(auto_delete(resp))
        return

    if indice < 1 or indice > len(lista):
        resp = await ctx.send(
            f"{ctx.author.mention} esse número não existe na sua playlist :("
        )
        asyncio.create_task(auto_delete(resp))
        return

    removida = lista.pop(indice - 1)
    resp = await ctx.send(
        embed=criar_embed_playlist(
            ctx,
            lista,
            destaque=(
                f"♡ Removi **{nome_musica(removida)}** da posição **{indice}** :3"
            ),
        )
    )
    asyncio.create_task(auto_delete(resp))


# INFO
# ──────────────────────────────────────────
@bot.command(name="info")
async def info(ctx):
    nome_servidor = ctx.guild.name if ctx.guild else "Servidor"
    quantidade_membros = ctx.guild.member_count if ctx.guild else "—"
    mensagem_info = (
        "୨୧・┈┈・୨୧・┈┈・୨୧\n\n"
        "🌸₊˚⊹ **Madoka • Madokinha** ⊹˚₊🌸\n"
        "꒰ა ♡ ໒꒱ **Oi amor, aqui está meu manualzinho! :3** 🎀\n\n"
        "╭・🌷 **INFORMAÇÕES DO SERVIDOR**\n"
        "│\n"
        f"├ 🏡 **Servidor:** {nome_servidor}\n"
        f"├ 👥 **Membros:** {quantidade_membros}\n"
        "├ 🎀 **Prefixo:** `ap!`\n"
        "╰ ✨ As respostas e embeds somem depois de alguns segundos!\n\n"
        "╭・🎀 **COMANDOS PARA MEMBROS**\n"
        "│\n"
        "├ 🎵 `ap!tocar <música>` — Procura uma versão em áudio e toca no canal de voz. "
        "Aceita nome, link do YouTube ou link público do Spotify.\n"
        "├ 🎧 `ap!entrar` — Entro no canal de voz em que você está.\n"
        "├ 🚪 `ap!sair` — Saio do canal e encerro a reprodução.\n"
        "├ ⏹️ `ap!parar` — Paro a música atual sem sair do canal.\n"
        "├ ⏭️ `ap!pular` — Pulo a música e toco o próximo item da sua playlist.\n"
        "├ 🎶 `ap!music` — Mostro capa, título, artista e progresso da música.\n"
        "├ 💿 `ap!playlist <música>` — Adiciono uma música à sua playlist pessoal (até 20 itens). "
        "Sem texto, mostro a playlist em ordem.\n"
        "├ 🗑️ `ap!deletelist <número>` — Removo o item escolhido da playlist.\n"
        "├ 😂 `ap!meme` — Envio um meme aleatório! Tem cooldown de 3 minutos.\n"
        "├ 📖 `ap!resumo [2-200]` — Resumo as últimas mensagens do canal.\n"
        "╰ 🖼️ `ap!avatar [@membro]` — Mostro o avatar de alguém.\n\n"
        "╭・👑 **COMANDOS PARA ADMINISTRADORES**\n"
        "│\n"
        "├ 🎟️ `ap!convite` — Envia convite por DM aos membros elegíveis.\n"
        "├ 💌 `ap!message <texto> + anexo` — Clona uma mensagem em uma embed rosa-bebê, "
        "incluindo a primeira imagem anexada.\n"
        "├ 🧹 `ap!limpar [quantidade]` — Apaga até 1000 mensagens. Requer **Gerenciar Mensagens**.\n"
        "├ 📋 `ap!clonar ID_ORIGEM ID_DESTINO` — Copia cargos, categorias e canais "
        "para outro servidor sem apagar nada.\n"
        "╰ 💌 `ap!dm @usuario <mensagem>` — Envia uma DM individual.\n\n"
        "╭・🌟 **OUTROS COMANDOS**\n"
        "│\n"
        "├ 🏓 `ap!ping` — Mostra a latência da Madoka.\n"
        "╰ 📚 `ap!info` — Abre este manual detalhado.\n\n"
        "╭・🔐 **IMPORTANTE**\n"
        "│\n"
        "├ 👑 Administradores também podem usar todos os comandos de membros.\n"
        "├ 🛡️ As permissões são verificadas pelo próprio Discord.\n"
        "├ 🚫 Os comandos não funcionam em DM.\n"
        "╰ 🌸 Novos membros recebem boas-vindas automaticamente no canal principal!\n\n"
        "₊˚⊹♡ **Obrigada por usar a Madoka!** ♡⊹˚₊\n"
        "🎀 *Criada pela Nica* 🎀\n\n"
        "୨୧・┈┈・୨୧・┈┈・୨୧"
    )
    embed = discord.Embed(
        description=mensagem_info,
        color=COR_ROSA_BEBE,
    )
    embed.set_footer(text="manual da Madoka • esta mensagem some em 45s")

    arquivo_info = None
    if os.path.isfile(INFO_IMAGE_PATH):
        arquivo_info = discord.File(INFO_IMAGE_PATH, filename="info.png")
        embed.set_image(url="attachment://info.png")

    if arquivo_info:
        resp = await ctx.send(embed=embed, file=arquivo_info)
    else:
        resp = await ctx.send(embed=embed)
    asyncio.create_task(auto_delete(resp, delay=45))


def copiar_overwrites(canal_origem, mapa_cargos):
    """Converte permissões de cargos da origem para cargos do destino."""
    overwrites = {}
    for alvo, overwrite in canal_origem.overwrites.items():
        if not isinstance(alvo, discord.Role):
            continue
        cargo_destino = mapa_cargos.get(alvo.id)
        if cargo_destino is None:
            continue
        allow, deny = overwrite.pair()
        overwrites[cargo_destino] = discord.PermissionOverwrite.from_pair(allow, deny)
    return overwrites


def encontrar_canal(destino, canal_origem, categoria_destino):
    for canal in destino.channels:
        if canal.name != canal_origem.name:
            continue
        if isinstance(canal_origem, discord.CategoryChannel):
            if isinstance(canal, discord.CategoryChannel):
                return canal
        elif isinstance(canal_origem, discord.VoiceChannel):
            if isinstance(canal, discord.VoiceChannel):
                if canal.category == categoria_destino:
                    return canal
        elif isinstance(canal_origem, discord.TextChannel):
            if isinstance(canal, discord.TextChannel):
                if canal.category == categoria_destino:
                    return canal
    return None


async def clonar_estrutura(origem, destino, aviso):
    """Copia cargos, categorias e canais sem remover nada do destino."""
    if not destino.me.guild_permissions.manage_roles:
        raise PermissionError("o bot precisa de Gerenciar cargos no destino")
    if not destino.me.guild_permissions.manage_channels:
        raise PermissionError("o bot precisa de Gerenciar canais no destino")

    mapa_cargos = {origem.default_role.id: destino.default_role}
    cargos_criados = 0
    cargos_ignorados = 0

    cargos = sorted(origem.roles, key=lambda cargo: cargo.position)
    for cargo_origem in cargos:
        if cargo_origem.is_default() or cargo_origem.managed:
            cargos_ignorados += 1
            continue

        cargo_destino = discord.utils.find(
            lambda cargo: cargo.name == cargo_origem.name,
            destino.roles,
        )
        if cargo_destino is None:
            cargo_destino = await destino.create_role(
                name=cargo_origem.name,
                permissions=cargo_origem.permissions,
                colour=cargo_origem.colour,
                hoist=cargo_origem.hoist,
                mentionable=cargo_origem.mentionable,
                reason="Clonagem de estrutura solicitada por administrador",
            )
            cargos_criados += 1
        else:
            cargos_ignorados += 1
        mapa_cargos[cargo_origem.id] = cargo_destino

    mapa_categorias = {}
    categorias_criadas = 0
    for categoria_origem in sorted(origem.categories, key=lambda categoria: categoria.position):
        existente = encontrar_canal(destino, categoria_origem, None)
        if existente:
            categoria_destino = existente
        else:
            categoria_destino = await destino.create_category(
                name=categoria_origem.name,
                overwrites=copiar_overwrites(categoria_origem, mapa_cargos),
                reason="Clonagem de estrutura solicitada por administrador",
            )
            categorias_criadas += 1
        mapa_categorias[categoria_origem.id] = categoria_destino

    canais_criados = 0
    canais_ignorados = 0
    canais_nao_suportados = 0
    canais = sorted(
        (
            canal for canal in origem.channels
            if not isinstance(canal, discord.CategoryChannel)
        ),
        key=lambda canal: (canal.category.position if canal.category else -1, canal.position),
    )
    for canal_origem in canais:
        categoria_destino = (
            mapa_categorias.get(canal_origem.category.id)
            if canal_origem.category
            else None
        )
        existente = encontrar_canal(destino, canal_origem, categoria_destino)
        if existente:
            canais_ignorados += 1
            continue

        overwrites = copiar_overwrites(canal_origem, mapa_cargos)
        if isinstance(canal_origem, discord.TextChannel):
            await destino.create_text_channel(
                name=canal_origem.name,
                category=categoria_destino,
                topic=canal_origem.topic,
                slowmode_delay=canal_origem.slowmode_delay,
                nsfw=canal_origem.nsfw,
                overwrites=overwrites,
                reason="Clonagem de estrutura solicitada por administrador",
            )
        elif isinstance(canal_origem, discord.VoiceChannel):
            await destino.create_voice_channel(
                name=canal_origem.name,
                category=categoria_destino,
                user_limit=canal_origem.user_limit,
                overwrites=overwrites,
                reason="Clonagem de estrutura solicitada por administrador",
            )
        else:
            canais_nao_suportados += 1
            continue
        canais_criados += 1

    return {
        "cargos_criados": cargos_criados,
        "cargos_ignorados": cargos_ignorados,
        "categorias_criadas": categorias_criadas,
        "canais_criados": canais_criados,
        "canais_ignorados": canais_ignorados,
        "canais_nao_suportados": canais_nao_suportados,
    }


@bot.command(name="clonar")
@commands.has_permissions(administrator=True)
async def clonar(ctx, origem_id: str | None = None, destino_id: str | None = None):
    if not origem_id or not destino_id:
        resp = await ctx.send(
            f"{ctx.author.mention} usa: `ap!clonar ID_ORIGEM ID_DESTINO` :b"
        )
        asyncio.create_task(auto_delete(resp))
        return

    try:
        id_origem = int(origem_id)
        id_destino = int(destino_id)
    except ValueError:
        resp = await ctx.send(f"{ctx.author.mention} os dois IDs precisam ser números :(")
        asyncio.create_task(auto_delete(resp))
        return

    origem = bot.get_guild(id_origem)
    destino = bot.get_guild(id_destino)
    if origem is None or destino is None:
        resp = await ctx.send(
            f"{ctx.author.mention} eu preciso estar nos dois servidores para copiar a estrutura."
        )
        asyncio.create_task(auto_delete(resp))
        return

    if origem.id == destino.id:
        resp = await ctx.send(f"{ctx.author.mention} origem e destino precisam ser diferentes :(")
        asyncio.create_task(auto_delete(resp))
        return

    if not ctx.guild or ctx.guild.id != destino.id:
        resp = await ctx.send(
            f"{ctx.author.mention} execute esse comando dentro do servidor de destino."
        )
        asyncio.create_task(auto_delete(resp))
        return

    aviso = await ctx.send(
        f"{ctx.author.mention} copiando a estrutura de **{origem.name}** "
        f"para **{destino.name}**... isso não apaga nada."
    )
    try:
        resultado = await clonar_estrutura(origem, destino, aviso)
        await aviso.edit(
            content=(
                f"{ctx.author.mention} estrutura copiada :3\n"
                f"Cargos criados: **{resultado['cargos_criados']}**\n"
                f"Categorias criadas: **{resultado['categorias_criadas']}**\n"
                f"Canais criados: **{resultado['canais_criados']}**\n"
                f"Já existentes/ignorados: **{resultado['canais_ignorados']}**\n"
                f"Tipos não suportados: **{resultado['canais_nao_suportados']}**"
            )
        )
    except (PermissionError, discord.Forbidden) as e:
        print(f"[ERRO CLONAR] {e}")
        await aviso.edit(
            content=f"{ctx.author.mention} não tenho permissões suficientes no destino :("
        )
    except discord.HTTPException as e:
        print(f"[ERRO CLONAR] {e}")
        await aviso.edit(content=f"{ctx.author.mention} o Discord recusou alguma criação :(")
    asyncio.create_task(auto_delete(aviso, delay=30))


@clonar.error
async def clonar_error(ctx, error):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if isinstance(error, commands.MissingPermissions):
        resp = await ctx.send(
            f"{ctx.author.mention} o comando `ap!clonar` é só para administradores."
        )
        asyncio.create_task(auto_delete(resp))


# ──────────────────────────────────────────
# VOZ — entrar / sair / tocar / parar / pular
# ──────────────────────────────────────────
@bot.command(name="entrar")
async def entrar(ctx):
    if not ctx.author.voice:
        resp = await ctx.send(
            f"{ctx.author.mention} você precisa estar em um canal de voz primeiro :3",
            delete_after=15,
        )
        asyncio.create_task(auto_delete(resp, delay=MUSICA_MENSAGEM_DELAY))
        return
    canal = ctx.author.voice.channel
    if ctx.voice_client:
        await ctx.voice_client.move_to(canal)
    else:
        await canal.connect()
    resp = await ctx.send(
        f"{ctx.author.mention} entrei em **{canal.name}** 💕",
        delete_after=15,
    )
    asyncio.create_task(auto_delete(resp, delay=MUSICA_MENSAGEM_DELAY))


@bot.command(name="sair")
async def sair(ctx):
    if ctx.voice_client:
        MUSICAS_ATUAIS.pop(ctx.guild.id if ctx.guild else 0, None)
        await ctx.voice_client.disconnect()
        resp = await ctx.send(
            f"{ctx.author.mention} saí do canal :3 tchau tchau",
            delete_after=15,
        )
    else:
        resp = await ctx.send(
            f"{ctx.author.mention} nem tô em canal nenhum :b",
            delete_after=15,
        )
    asyncio.create_task(auto_delete(resp, delay=MUSICA_MENSAGEM_DELAY))


@bot.command(name="tocar")
async def tocar(ctx, *, busca: str | None = None):
    if not busca:
        resp = await ctx.send(
            f"{ctx.author.mention} me fala o que tocar! "
            f"Ex: `ap!tocar nome da música` ou cola link do YouTube/Spotify"
        )
        return

    try:
        carregar_opus()
    except Exception as e:
        print(f"[ERRO OPUS] {e}")
        resp = await ctx.send(
            f"{ctx.author.mention} o áudio não está disponível agora :("
        )
        return

    if not ctx.voice_client:
        if not ctx.author.voice:
            resp = await ctx.send(
                f"{ctx.author.mention} entra em um canal de voz primeiro :3"
            )
            return
        await ctx.author.voice.channel.connect()

    aviso = await ctx.send(
        f"{ctx.author.mention} procurando **{busca}**... 🔥"
    )

    preparada = await preparar_busca(busca.strip())
    if not preparada:
        await aviso.edit(content=f"{ctx.author.mention} não consegui resolver esse link do Spotify :(")
        return
    query, origem, spotify = preparada

    try:
        loop = asyncio.get_event_loop()
        (
            arquivo_audio,
            titulo_youtube,
            artista_youtube,
            thumbnail_youtube,
            duracao,
            pasta_temporaria,
        ) = await loop.run_in_executor(
            None, _baixar_audio_sync, query
        )
    except Exception as e:
        print(f"[ERRO TOCAR] {e}")
        await aviso.edit(content=f"{ctx.author.mention} não consegui baixar esse áudio :(")
        return

    titulo = spotify.get("title") if spotify else titulo_youtube
    artista = spotify.get("artist") if spotify else artista_youtube
    capa_preferida = (
        spotify.get("thumbnail")
        if spotify and spotify.get("thumbnail")
        else None
    )
    capa_album, album, artista_album = await buscar_capa_album(
        titulo,
        artista,
        capa_atual=capa_preferida,
    )
    artista = artista or artista_album
    thumbnail = capa_album or thumbnail_youtube
    vc = ctx.voice_client
    if vc.is_playing():
        vc.stop()

    guild_id = ctx.guild.id if ctx.guild else 0
    estado = {
        "title": titulo,
        "artist": artista,
        "album": album,
        "thumbnail": thumbnail,
        "duration": duracao,
        "source": origem,
        "started_at": time.monotonic(),
        "requested_by": ctx.author.display_name,
    }

    def finalizar_audio(error):
        """Limpa o download e avisa no Discord se o FFmpeg falhar."""
        shutil.rmtree(pasta_temporaria, ignore_errors=True)
        if not error:
            if MUSICAS_ATUAIS.get(guild_id) is estado:
                MUSICAS_ATUAIS.pop(guild_id, None)
            return

        print(f"[VOZ] reprodução encerrada com erro: {error}")
        if MUSICAS_ATUAIS.get(guild_id) is estado:
            MUSICAS_ATUAIS.pop(guild_id, None)
        try:
            futuro = asyncio.run_coroutine_threadsafe(
                aviso.edit(
                    content=f"{ctx.author.mention} o áudio falhou durante a reprodução :("
                ),
                bot.loop,
            )
            futuro.result(timeout=5)
        except Exception as callback_error:
            print(f"[VOZ] não consegui atualizar o aviso: {callback_error}")

    try:
        # O arquivo local evita 403 de URLs temporárias do YouTube no FFmpeg.
        source = discord.FFmpegPCMAudio(
            source=arquivo_audio,
            executable="ffmpeg",
            options=FFMPEG_OPTIONS,
        )
        vc.play(
            source,
            after=finalizar_audio,
        )
        MUSICAS_ATUAIS[guild_id] = estado
    except Exception as e:
        print(f"[ERRO ÁUDIO] {e}")
        shutil.rmtree(pasta_temporaria, ignore_errors=True)
        await aviso.edit(content=f"{ctx.author.mention} não consegui iniciar o áudio :(")
        return

    await aviso.edit(
        content=None,
        embed=criar_embed_musica(estado),
    )


@bot.command(name="music")
async def music(ctx):
    guild_id = ctx.guild.id if ctx.guild else 0
    estado = MUSICAS_ATUAIS.get(guild_id)
    if not estado or not ctx.voice_client or not ctx.voice_client.is_playing():
        MUSICAS_ATUAIS.pop(guild_id, None)
        resp = await ctx.send(
            f"{ctx.author.mention} não tem nenhuma música tocando agora :b"
        )
        return

    decorrido = max(0, int(time.monotonic() - estado["started_at"]))
    embed = criar_embed_musica(estado, decorrido)
    await ctx.send(embed=embed)


@bot.command(name="parar")
async def parar(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        MUSICAS_ATUAIS.pop(ctx.guild.id if ctx.guild else 0, None)
        ctx.voice_client.stop()
        resp = await ctx.send(f"{ctx.author.mention} parei a música :3")
    else:
        resp = await ctx.send(f"{ctx.author.mention} não tô tocando nada :b")


@bot.command(name="pular")
async def pular(ctx):
    lista = PLAYLISTS.get(chave_playlist(ctx), [])
    proxima = lista.pop(0) if lista else None

    if ctx.voice_client and ctx.voice_client.is_playing():
        MUSICAS_ATUAIS.pop(ctx.guild.id if ctx.guild else 0, None)
        ctx.voice_client.stop()

    if proxima:
        resp = await ctx.send(
            f"{ctx.author.mention} pulei! Vou tocar **{nome_musica(proxima)}** agora :D"
        )
        await tocar.callback(ctx, busca=proxima["query"])
        return

    resp = await ctx.send(
        f"{ctx.author.mention} não tem próxima música na sua playlist. "
        "Adiciona uma com `ap!playlist <música>` :b"
    )


@bot.event
async def on_command_error(ctx, error):
    """Corrige erros comuns de digitação sem deixar comandos silenciosos."""
    if isinstance(error, commands.CommandNotFound):
        invocado = (ctx.invoked_with or "").casefold()

        try:
            await ctx.message.delete()
        except Exception:
            pass

        if invocado in {"imagem", "imagems", "image", "images"}:
            texto = (
                f"{ctx.author.mention} reconheci o que você quis dizer: "
                "`ap!imagem`. Esse comando foi removido; para enviar uma imagem "
                "como embed, use `ap!message` com o anexo :3"
            )
        else:
            nomes = [
                comando.name
                for comando in bot.commands
                if not comando.hidden
            ]
            sugestao = difflib.get_close_matches(invocado, nomes, n=1, cutoff=0.58)
            if sugestao:
                texto = (
                    f"{ctx.author.mention} acho que você quis dizer "
                    f"`ap!{sugestao[0]}` :3"
                )
            else:
                texto = (
                    f"{ctx.author.mention} não reconheci esse comando. "
                    "Usa `ap!info` para ver os comandos disponíveis :b"
                )

        resp = await ctx.send(texto)
        asyncio.create_task(auto_delete(resp))
        return

    if (
        isinstance(error, commands.BadArgument)
        and ctx.command
        and ctx.command.name == "resumo"
    ):
        resp = await ctx.send(
            f"{ctx.author.mention} o resumo aceita um número entre 2 e 200. "
            "Ex: `ap!resumo 10`"
        )
        asyncio.create_task(auto_delete(resp))
        return

    if isinstance(error, commands.NoPrivateMessage):
        resp = await ctx.send(
            f"{ctx.author.mention} esse comando só funciona dentro de um servidor Discord."
        )
        asyncio.create_task(auto_delete(resp))
        return

    if isinstance(error, commands.MissingPermissions):
        resp = await ctx.send(
            f"{ctx.author.mention} você não tem a permissão do Discord necessária para esse comando."
        )
        asyncio.create_task(auto_delete(resp))
        return

    print(f"[ERRO COMANDO] {error}")


if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN não encontrado.")
    else:
        bot.run(TOKEN)
