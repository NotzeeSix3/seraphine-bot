# ============================================================
#  BOT DISCORD AI — SERAPHINE (PROFESSIONAL VERSION)
# ============================================================
#
#  SETUP:
#  1. Buat file .env di folder yang sama dengan bot.py
#  2. Isi DISCORD_TOKEN, OPENROUTER_API_KEY, NEWSAPI_KEY
#  3. pip install discord.py requests python-dotenv
#  4. python bot.py
#
# ============================================================

import discord
from discord import app_commands
import requests
import sqlite3
import logging
import os
import random
import ssl
import certifi
ssl._create_default_https_context = ssl._create_unverified_context
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ["PATH"] += os.pathsep + os.getcwd()
import asyncio
import yt_dlp
from datetime import datetime, timedelta
from dotenv import load_dotenv
from collections import defaultdict
import time
import traceback
from better_profanity import profanity

# ============================================================
#  MUSIC PLAYER CONFIG
# ============================================================

ytdl_log = logging.getLogger(__name__)

def _get_cookies_file():
    """Ambil cookies YouTube dari env var YOUTUBE_COOKIES (b64 atau raw) atau file cookies.txt."""
    import base64
    env_cookies = (os.environ.get('YOUTUBE_COOKIES') or os.environ.get('YOUTUBE_COOKIES_B64') or '').strip()
    if env_cookies:
        try:
            path = os.path.join(os.getcwd(), '.ytdlp_cookies.txt')
            content = env_cookies
            try:
                decoded = base64.b64decode(env_cookies.encode('utf-8')).decode('utf-8')
                if '.youtube.com' in decoded:
                    content = decoded
            except Exception:
                pass
            
            content = content.replace('\\n', '\n')
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(content)
            return path
        except Exception as e:
            ytdl_log.warning(f"Gagal menulis YOUTUBE_COOKIES ke file: {e}")
    for candidate in ('cookies.txt', '.ytdlp_cookies.txt'):
        if os.path.exists(candidate):
            return candidate
    return None

COOKIES_FILE = _get_cookies_file()
if COOKIES_FILE:
    ytdl_log.info(f"Menggunakan cookies YouTube dari: {COOKIES_FILE}")

def _make_ytdl(player_clients=None):
    cookies_file = _get_cookies_file()
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }
    if player_clients:
        opts['extractor_args'] = {'youtube': {'player_client': player_clients}}
    if cookies_file:
        opts['cookiefile'] = cookies_file
    return yt_dlp.YoutubeDL(opts)

ytdl = _make_ytdl()
ytdl = _make_ytdl()

# Urutan fallback player_client YouTube (Sep 2026): dari IP datacenter Railway,
# client 'web' default & 'tv' selalu kena bot-check/403; yang terbukti paling
# robust dari uji lokal: 'android' (beri direct stream URL mp4, bypass SABR),
# lalu 'tv_embedded'/'web_embedded'. 'default' dipindah ke akhir sebagai opsi terakhir.
_YTDL_FALLBACK_CLIENTS = [
    ['android'],
    ['android', 'ios'],
    ['web_embedded'],
    ['tv_embedded'],
    ['tv'],
    ['mweb'],
    [None],  # default (web) — terakhir karena URL-nya sering 403 walau ekstraksi sukses
]

_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "confirm your age",
    "age-restricted",
    "request was sent to youtube",
    "403",
    "forbidden",
)

_FFMPEG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _probe_stream_url(url):
    """HEAD/Range GET kecil ke URL stream: True kalau googlevideo nerima (bukan 403)."""
    try:
        r = requests.get(url, headers={'User-Agent': _FFMPEG_UA},
                         stream=True, timeout=8, allow_redirects=True)
        ok = r.status_code == 200
        r.close()
        return ok
    except Exception:
        return False


def _extract_info_with_fallback(url, download):
    """extract_info dengan retry pakai player_client alternatif kalau kena bot-check
    YouTube ATAU URL stream-nya ditolak (403) saat mau di-download/stream ffmpeg."""
    attempts = [None] + _YTDL_FALLBACK_CLIENTS
    last_err = None
    for clients in attempts:
        extractor = ytdl if clients is None else _make_ytdl(clients)
        try:
            data = extractor.extract_info(url, download=download)
            if data and not download and 'entries' not in data:
                # Stream mode: pastiin URL-nya gak 403 sebelum dikasih ke ffmpeg.
                stream_url = data.get('url')
                if stream_url and not _probe_stream_url(stream_url):
                    ytdl_log.warning(
                        f"URL stream ditolak (403) untuk player_client={clients}, fallback ke client lain")
                    continue
            return data
        except Exception as e:
            last_err = e
            msg = str(e or '').lower()
            if any(marker in msg for marker in _BOT_CHECK_MARKERS):
                ytdl_log.warning(f"YouTube bot-check/age-gate, coba fallback player_client={clients}")
                continue
            if 'no longer valid' in msg or 'rotated' in msg or 'reloaded' in msg:
                ytdl_log.warning(
                    f"YouTube cookie kemungkinan KADALUARSA/dirotasi (msg={msg[:80]}). "
                    f"REFRESH YOUTUBE_COOKIES di Railway dashboard supaya bot-check lobos.")
                continue
    raise last_err

ffmpeg_options = {
    'options': '-vn',
    'before_options': ('-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
                       f'-user_agent "{_FFMPEG_UA}"'),
}

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        
        if not url.startswith(('http://', 'https://', 'www.')):
            if not url.startswith('ytsearch'):
                url = f"ytsearch1:{url}"

        try:
            data = await loop.run_in_executor(None, lambda: _extract_info_with_fallback(url, download=False))
            if 'entries' in data:
                data = data['entries'][0]
                if not data.get('url'):
                    video_url = data.get('webpage_url') or f"https://www.youtube.com/watch?v={data.get('id')}"
                    data = await loop.run_in_executor(None, lambda: _extract_info_with_fallback(video_url, download=False))
                    if 'entries' in data:
                        data = data['entries'][0]
            filename = data.get('url')
            if not filename:
                raise Exception("No stream URL")
        except Exception as e:
            logger.error(f"from_url stream-retry menuju download=True: {e}")
            data = await loop.run_in_executor(None, lambda: _extract_info_with_fallback(url, download=True))
            if not data:
                raise Exception("YouTube extraction kosong total (semua player_client gagal) — cek YOUTUBE_COOKIES / koneksi")
            if 'entries' in data:
                data = data['entries'][0]
            if not data:
                raise Exception("YouTube extraction kosong (entries kosong)")
            filename = ytdl.prepare_filename(data)

        if not filename:
            raise Exception("Gagal mendapatkan URL stream / file dari YouTube.")
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

# Antrean musik per server
music_queues = defaultdict(list)
music_loop = defaultdict(bool)  # guild_id -> loop current track

_MUSIC_ERROR_HINTS = [
    ("no longer valid", "🍪 Cookie YouTube kedaluwarsa/dirotasi — Notzee harus refresh YOUTUBE_COOKIES di Railway."),
    ("rotated", "🍪 Cookie YouTube kedaluwarsa/dirotasi — refresh YOUTUBE_COOKIES di Railway."),
    ("sign in to confirm", "🤖 YouTube minta verifikasi bot-check — coba lagi nanti / refresh cookies."),
    ("bot-check", "🤖 YouTube nge-flag akses bot — coba lagi nanti / refresh cookies."),
    ("age", "🔞 Video kena age-gate, butuh akses khusus."),
    ("private", "🔒 Video private atau udah dihapus."),
    ("unavailable", "🚫 Video unavailable (region/warna-hapus)."),
    ("403", "🚫 Stream ditolak server (403) — coba skip atau mainin lagu lain."),
    ("ffmpeg", "⚙️ FFmpeg error internal — coba skip ke lagu berikutnya."),
]

def _translate_music_error(e: Exception) -> str:
    """Map raw yt-dlp/ffmpeg error ke pesan user yang actionable."""
    msg = str(e or "").lower()
    for marker, hint in _MUSIC_ERROR_HINTS:
        if marker in msg:
            return hint
    return f"❌ Gagal putar musik: {str(e)[:120]}"


class MusicControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            await interaction.response.send_message("❌ Bot lagi gak ada di voice channel.", ephemeral=True)
            return
        if voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("⏸️ Musik dipause.", ephemeral=True)
        elif voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("▶️ Musik dilanjutkan.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Gak ada musik yang lagi diputar.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
            await interaction.response.send_message("⏭️ Lagu di-skip!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Gak ada lagu yang lagi diputar.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client:
            await voice_client.disconnect()
            music_queues[self.guild_id] = []
            await interaction.response.send_message("🛑 Musik dihentikan dan bot disconnect.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Bot lagi gak ada di channel.", ephemeral=True)

AUTOPLAY_POOL = [
    "lofi hip hop radio - beats to relax/study to",
    "chill lofi mix relax music",
    "dj remix viral terbaru enak",
    "synthwave radio - chill synth / retro beats",
    "acoustic guitar pop hits cover",
    "japanese city pop mix",
    "top hits indonesia santai",
]

async def _autoplay_next(guild_id, voice_client, channel):
    if not voice_client or not voice_client.is_connected():
        return
    query = random.choice(AUTOPLAY_POOL)
    try:
        search_query = f"ytsearch1:{query}"
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: _extract_info_with_fallback(search_query, download=False))
        if 'entries' in data:
            data = data['entries'][0]
            if not data.get('url'):
                video_url = data.get('webpage_url') or f"https://www.youtube.com/watch?v={data.get('id')}"
                data = await loop.run_in_executor(None, lambda: _extract_info_with_fallback(video_url, download=False))
                if 'entries' in data:
                    data = data['entries'][0]
        
        webpage_url = data.get('webpage_url') or f"https://www.youtube.com/watch?v={data.get('id')}"
        if not webpage_url:
            return
            
        player = await YTDLSource.from_url(webpage_url, loop=client.loop, stream=True)
        if voice_client and not voice_client.is_playing():
            voice_client.play(player, after=lambda e: play_next(guild_id, voice_client, channel))
            view = MusicControlView(guild_id)
            embed = discord.Embed(
                title="🎵 Autoplay (Musik Random Rekomendasi)",
                description=f"Antrean habis, bot otomatis memutar: [{player.title}]({player.url})",
                color=0x7289da
            )
            await channel.send(embed=embed, view=view)
    except Exception as e:
        logger.error(f"Autoplay error: {e}")

def play_next(guild_id, voice_client, channel):
    # Loop aktif: replay source terakhir (disimpan di atribut voice_client)
    if music_loop.get(guild_id) and voice_client.is_connected():
        src = getattr(voice_client, "_last_source", None)
        if src is not None:
            try:
                voice_client.play(src, after=lambda e: play_next(guild_id, voice_client, channel))
                return
            except Exception as e:
                logger.error(f"Loop replay error: {e}")
    if music_queues[guild_id]:
        next_player = music_queues[guild_id].pop(0)
        try:
            voice_client._last_source = next_player
            voice_client.play(next_player, after=lambda e: play_next(guild_id, voice_client, channel))
            view = MusicControlView(guild_id)
            future = asyncio.run_coroutine_threadsafe(
                channel.send(embed=discord.Embed(
                    title="🎵 Sekarang Diputar (Dari Antrean)",
                    description=f"[{next_player.title}]({next_player.url})",
                    color=0x7289da
                ), view=view),
                client.loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"Error in play_next: {e}")
    else:
        asyncio.run_coroutine_threadsafe(_autoplay_next(guild_id, voice_client, channel), client.loop)

# Load environment variables
load_dotenv()

# ============================================================
#  CONFIGURATION
# ============================================================

def load_bot_config():
    if not os.path.exists("config.json"):
        return {
            "ai_chat_enabled": True,
            "music_enabled": True,
            "automod_enabled": True,
            "voice_log_enabled": True
        }
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except:
        return {
            "ai_chat_enabled": True,
            "music_enabled": True,
            "automod_enabled": True,
            "voice_log_enabled": True
        }

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()
MOD_LOG_CHANNEL_NAME = os.getenv("MOD_LOG_CHANNEL", "moderator-only").strip()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NEWSAPI_BASE_URL = "https://newsapi.org/v2"
AI_MODEL = "deepseek/deepseek-chat"

DB_NAME = "bot_memory.db"
MAX_HISTORY_MESSAGES = 2  # Context messages (optimized)
MAX_DB_MESSAGES = 20  # Total stored per user
PREFIX = "!"
RATE_LIMIT_SECONDS = 5  # Per user rate limit
RESPONSE_CHAR_LIMIT = 1900  # Discord message limit

# Store mod log channels per guild
mod_log_channels = {}

# Personality
KEPRIBADIAN = (
    "Kamu adalah bot Discord bernama Seraphine AI yang asik, santai, dan ramah. "
    "Nama mu adalah Seraphine AI. Jika ditanya siapa nama mu atau siapa kamu, jawab 'Saya adalah Seraphine AI'. "
    "Pembuat mu adalah Notzee - hanya sebut ini jika ditanya langsung siapa pembuat mu. "
    "PENTING SEKALI: Di SETIAP jawaban, MULAI dengan menyebutkan nama user yang bertanya. Contoh: 'Yo {username}, ...' atau '{username}, itu dia ...'. "
    "PENTING: Ketika menjawab pertanyaan, berikan jawaban yang DETAIL, PANJANG, dan MENJELASKAN dengan baik. "
    "Berikan penjelasan yang komprehensif, contoh konkret jika diperlukan. "
    "PENTING: Ketika diminta buatin code/coding, LANGSUNG berikan code lengkap dengan format code block (```python atau ```javascript dll). "
    "Jawab pakai bahasa Indonesia yang gaul tapi sopan. "
    "Prioritaskan kualitas jawaban dan detail penjelasan daripada singkat."
)

# ============================================================
#  LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
#  PROFANITY FILTER SETUP
# ============================================================

profanity.load_censor_words()

# Custom toxic keywords untuk Indonesia
CUSTOM_TOXIC_WORDS = [
    "anjing", "babi", "monyet", "setan", "bangsat", "kontol", "memek", 
    "biadab", "tolol", "dungu", "goblok", "bodoh", "sampah", "hina",
    "jelek", "buruk", "sial", "sinting", "sarap"
]

# Add custom words to profanity filter
profanity.add_censor_words(CUSTOM_TOXIC_WORDS)

def contains_toxic(text: str) -> bool:
    """Check if text contains toxic content (hybrid method)."""
    text_lower = text.lower()
    
    # Method 1: better-profanity library check
    if profanity.contains_profanity(text):
        return True
    
    # Method 2: Custom keyword check
    for word in CUSTOM_TOXIC_WORDS:
        if word in text_lower:
            return True
    
    return False

async def get_or_create_mod_log_channel(guild: discord.Guild) -> discord.TextChannel:
    """Get existing mod log channel or create if not exists."""
    try:
        # Check if already cached
        if guild.id in mod_log_channels:
            channel = guild.get_channel(mod_log_channels[guild.id])
            if channel:
                return channel
        
        # Look for existing moderator-only or mod-logs channel
        for channel in guild.text_channels:
            if channel.name in [MOD_LOG_CHANNEL_NAME, "mod-logs", "moderator-only"]:
                mod_log_channels[guild.id] = channel.id
                logger.info(f"Found existing mod log channel in {guild.name}")
                return channel
        
        # Create new moderator-only channel if not exists
        logger.info(f"Creating mod log channel in {guild.name}")
        
        # Create with restricted permissions (admin/mod only)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
        }
        
        channel = await guild.create_text_channel(
            MOD_LOG_CHANNEL_NAME,
            overwrites=overwrites,
            topic="🔒 Moderator logs - Toxic messages, kicks, warnings, voice movements"
        )
        
        mod_log_channels[guild.id] = channel.id
        logger.info(f"Created new mod log channel in {guild.name}")
        return channel
        
    except discord.Forbidden:
        logger.error(f"No permission to create channel in {guild.name}")
        return None
    except Exception as e:
        logger.error(f"Error creating mod log channel: {e}")
        return None

async def log_toxic_message(guild: discord.Guild, user: discord.Member, message_content: str, channel_name: str):
    """Log toxic message to mod log channel."""
    try:
        mod_channel = await get_or_create_mod_log_channel(guild)
        if not mod_channel:
            logger.warning("Could not get mod log channel")
            return
        
        embed = discord.Embed(
            title="⚠️ Toxic Message Detected",
            color=0xFF5733,
            timestamp=datetime.now()
        )
        
        embed.add_field(name="👤 User", value=f"{user.mention} ({user.name}#{user.discriminator})", inline=False)
        embed.add_field(name="💬 Channel", value=f"#{channel_name}", inline=False)
        embed.add_field(name="📝 Message", value=f"```{message_content[:500]}```" if len(message_content) < 500 else f"```{message_content[:497]}...```", inline=False)
        embed.add_field(name="🆔 User ID", value=str(user.id), inline=True)
        embed.add_field(name="⏰ Time", value=datetime.now().strftime("%d %b %Y %H:%M:%S"), inline=True)
        
        embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
        
        await mod_channel.send(embed=embed)
        logger.info(f"Toxic message logged from {user.name} in {guild.name}")
        
    except Exception as e:
        logger.error(f"Error logging toxic message: {e}")

# ============================================================
#  RATE LIMITER
# ============================================================

user_last_request = defaultdict(float)

def check_rate_limit(user_id: int) -> bool:
    """Check if user has exceeded rate limit."""
    current_time = time.time()
    last_request = user_last_request.get(user_id, 0)
    
    if current_time - last_request < RATE_LIMIT_SECONDS:
        return False
    
    user_last_request[user_id] = current_time
    return True

# ============================================================
#  ANTI-SPAM (flood / invite / mass-mention)
# ============================================================

import re as _re
_INVITE_RE = _re.compile(r'(discord\.(gg|io|me|li)|discordapp\.com/invite)/[\w-]+', _re.IGNORECASE)

# user_id -> list of (timestamp, message, channel_id)
_spam_tracker = defaultdict(list)
_spam_warned = defaultdict(float)

SPAM_WINDOW = 8          # seconds
SPAM_MAX_MSGS = 6        # messages within window = flood
SPAM_MENTION_LIMIT = 5   # unique mentions per message
SPAM_WARN_COOLDOWN = 30  # don't re-warn same user within N seconds

def _is_whitelisted_channel(channel_id: int) -> bool:
    return False

def _register_and_check_spam(user_id: int, content: str, mention_count: int, channel_id: int) -> str | None:
    """Register a message; return 'flood' | 'mention' | None. Invite is checked separately."""
    now = time.time()
    bucket = _spam_tracker[user_id]
    bucket[:] = [(t, m, c) for (t, m, c) in bucket if now - t <= SPAM_WINDOW]
    bucket.append((now, content, channel_id))
    if len(bucket) >= SPAM_MAX_MSGS:
        bucket.clear()
        return "flood"
    if mention_count > SPAM_MENTION_LIMIT:
        return "mention"
    return None

def _should_warn_spam(user_id: int) -> bool:
    now = time.time()
    if now - _spam_warned[user_id] < SPAM_WARN_COOLDOWN:
        return False
    _spam_warned[user_id] = now
    return True

async def _handle_spam_violation(pesan, violation: str) -> None:
    """Warn + auto-timeout on repeat offense; delete message."""
    member = pesan.author
    try:
        await pesan.delete()
    except Exception:
        pass
    add_infraction(pesan.guild.id, member.id, pesan.guild.me.id, "SPAM", f"{violation} in #{pesan.channel.name}")
    warns = count_infractions(pesan.guild.id, member.id, "SPAM")
    try:
        if warns >= 3:
            until = discord.utils.utcnow() + timedelta(minutes=30)
            await member.timeout(until, reason=f"Anti-spam: {warns} violations")
            add_infraction(pesan.guild.id, member.id, pesan.guild.me.id, "AUTO-TIMEOUT", f"Anti-spam: {warns} violations")
            note = f"🛑 **{member.mention}** kena timeout 30 menit (pelanggaran spam ke-{warns})."
        elif _should_warn_spam(member.id):
            note = f"⚠️ {member.mention} jangan spam bro ({violation}). Pelanggaran ke-{warns}/3."
        else:
            note = None
        if note:
            await pesan.channel.send(note, delete_after=10)
    except Exception as e:
        logger.error(f"Anti-spam enforcement error: {e}")

# ============================================================
#  DISCORD CLIENT SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.voice_states = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ============================================================
#  VOICE MODERATION COMMANDS (VMUTE / VUNMUTE)
# ============================================================

@tree.command(name="vmute", description="Mute member di voice channel secara paksa")
@app_commands.describe(member="Member yang mau di-mute", reason="Alasan mute")
@app_commands.checks.has_permissions(mute_members=True)
async def slash_vmute(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.guild:
        await interaction.response.send_message("❌ Perintah ini cuma bisa dipakai di server Discord bro!", ephemeral=True)
        return

    if not member.voice or not member.voice.channel:
        await interaction.response.send_message(f"❌ **{member.display_name}** lagi gak ada di voice channel mana pun!", ephemeral=True)
        return

    try:
        await member.edit(mute=True, reason=reason)
        embed = discord.Embed(
            title="🔇 Voice Muted",
            description=f"Berhasil server-mute **{member.mention}** di voice channel **{member.voice.channel.name}**.\n📝 **Alasan:** {reason}",
            color=0xFF5733,
            timestamp=datetime.now()
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        logger.info(f"User {member.name} muted in voice by {interaction.user.name}")
    except Exception as e:
        logger.error(f"Error in vmute: {e}")
        await interaction.response.send_message(f"❌ Gagal nge-mute member: {e}", ephemeral=True)

@tree.command(name="vunmute", description="Lepas mute member di voice channel")
@app_commands.describe(member="Member yang mau di-unmute")
@app_commands.checks.has_permissions(mute_members=True)
async def slash_vunmute(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message("❌ Perintah ini cuma bisa dipakai di server Discord bro!", ephemeral=True)
        return

    if not member.voice or not member.voice.channel:
        await interaction.response.send_message(f"❌ **{member.display_name}** lagi gak ada di voice channel!", ephemeral=True)
        return

    try:
        await member.edit(mute=False)
        embed = discord.Embed(
            title="🔊 Voice Unmuted",
            description=f"Berhasil melepas server-mute untuk **{member.mention}** di voice channel **{member.voice.channel.name}**.",
            color=0x2ECC71,
            timestamp=datetime.now()
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        logger.info(f"User {member.name} unmuted in voice by {interaction.user.name}")
    except Exception as e:
        logger.error(f"Error in vunmute: {e}")
        await interaction.response.send_message(f"❌ Gagal melepas mute member: {e}", ephemeral=True)

# ============================================================
#  SLASH MODERATION + SERVER INFO COMMANDS
# ============================================================

async def _send_mod_log(guild: discord.Guild, embed: discord.Embed):
    """Best-effort delivery of a moderation embed to the mod log channel."""
    try:
        channel = await get_or_create_mod_log_channel(guild)
        if channel:
            await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Could not write to mod log: {e}")

@tree.command(name="skick", description="Kick member dari server (Admin/Mod)")
@app_commands.describe(member="Member yang mau di-kick", reason="Alasan kick")
@app_commands.checks.has_permissions(kick_members=True)
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ Gak bisa kick diri sendiri bro.", ephemeral=True)
        return

    if member.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ Posisi role member lebih tinggi/sama dengan bot, gak bisa di-kick.", ephemeral=True)
        return

    await interaction.response.defer()
    success, msg = await kick_user(member, reason, moderator_id=interaction.user.id)

    embed = discord.Embed(
        title="🚪 Kick Action",
        description=msg,
        color=0xFF5733 if success else 0xFF0000,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"Moderator: {interaction.user.name}")
    await interaction.followup.send(embed=embed)

    if success:
        await _send_mod_log(interaction.guild, embed)

@tree.command(name="sinfractions", description="Lihat riwayat moderasi member (Admin/Mod)")
@app_commands.describe(member="Member yang mau dicek riwayatnya")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_infractions(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    infractions = get_user_infractions(interaction.guild.id, member.id)

    if not infractions:
        embed = discord.Embed(
            title="📋 User Infractions",
            description=f"✅ {member.mention} belum punya riwayat pelanggaran.",
            color=0x00FF00,
            timestamp=datetime.now()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"📋 Infractions - {member.display_name}",
        description=f"Total pelanggaran: **{len(infractions)}**",
        color=0xFF5733,
        timestamp=datetime.now()
    )
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    for i, (action_type, reason, timestamp, mod_id) in enumerate(infractions[-10:], 1):
        embed.add_field(
            name=f"{i}. {action_type}",
            value=f"**Alasan:** {reason}\n**Mod:** <@{mod_id}>\n**Tanggal:** {timestamp}",
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)

# ============================================================
#  WARN / TIMEOUT / BAN + AUTO-ESCALATION
# ============================================================

def count_infractions(guild_id: int, user_id: int, action_type: str = "WARN") -> int:
    """Count infractions of a given type for a user in a guild."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''SELECT COUNT(*) FROM infractions
                     WHERE guild_id = ? AND user_id = ? AND action_type = ?''',
                  (guild_id, user_id, action_type))
        n = c.fetchone()[0]
        conn.close()
        return n
    except Exception as e:
        logger.error(f"Error counting infractions: {e}")
        return 0

async def _auto_escalate(interaction: discord.Interaction, member: discord.Member, reason: str):
    """3 warns = 1 hour timeout, 6 warns = kick. Returns escalation note or None."""
    warns = count_infractions(interaction.guild.id, member.id, "WARN")
    if warns >= 6 and not member.bot:
        success, msg = await kick_user(member, f"Auto-escalation: {warns} warnings", moderator_id=interaction.user.id)
        return f"⚠️ **Auto-escalation:** {warns} warn tercapai — member di-kick. {msg}"
    if warns >= 3:
        try:
            until = discord.utils.utcnow() + timedelta(hours=1)
            await member.timeout(until, reason=f"Auto-escalation: {warns} warnings")
            add_infraction(interaction.guild.id, member.id, interaction.user.id, "AUTO-TIMEOUT", f"Auto-escalation: {warns} warnings | {reason}")
            return f"⚠️ **Auto-escalation:** {warns} warn tercapai — member di-timeout 1 jam."
        except Exception as e:
            logger.error(f"Auto-escalation timeout failed: {e}")
            return f"⚠️ Auto-escalation gagal eksekusi timeout: {str(e)[:60]}"
    return None

@tree.command(name="swarn", description="Beri warning ke member (Admin/Mod)")
@app_commands.describe(member="Member yang mau di-warn", reason="Alasan warning")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ Gak bisa warn diri sendiri bro.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("❌ Gak bisa warn bot bro.", ephemeral=True)
        return

    add_infraction(interaction.guild.id, member.id, interaction.user.id, "WARN", reason)
    escalation = await _auto_escalate(interaction, member, reason)

    warns = count_infractions(interaction.guild.id, member.id, "WARN")
    embed = discord.Embed(
        title="⚠️ Warning",
        description=f"**{member.mention}** di-warn.\n📝 **Alasan:** {reason}\n📊 **Total warn:** {warns}/6",
        color=0xFFA500,
        timestamp=datetime.now()
    )
    if escalation:
        embed.add_field(name="Auto-Escalation", value=escalation, inline=False)
    embed.set_footer(text=f"Moderator: {interaction.user.name}")
    await interaction.response.send_message(embed=embed)
    await _send_mod_log(interaction.guild, embed)

@tree.command(name="stimeout", description="Timeout member (Admin/Mod)")
@app_commands.describe(member="Member yang mau di-timeout", minutes="Durasi timeout (menit)", reason="Alasan timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def slash_timeout(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ Gak bisa timeout diri sendiri bro.", ephemeral=True)
        return
    if not 1 <= minutes <= 40320:
        await interaction.response.send_message("❌ Durasi 1 - 40320 menit (28 hari max).", ephemeral=True)
        return

    try:
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)
        add_infraction(interaction.guild.id, member.id, interaction.user.id, "TIMEOUT", f"{minutes}m | {reason}")
        embed = discord.Embed(
            title="🔇 Timeout",
            description=f"**{member.mention}** di-timeout **{minutes} menit**.\n📝 **Alasan:** {reason}",
            color=0xFF5733,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Moderator: {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        await _send_mod_log(interaction.guild, embed)
    except Exception as e:
        logger.error(f"Timeout error: {e}")
        await interaction.response.send_message(f"❌ Gagal timeout: {str(e)[:80]}", ephemeral=True)

@tree.command(name="sban", description="Ban member dari server (Admin)")
@app_commands.describe(member="Member yang mau di-ban", reason="Alasan ban", delete_days="Hapus riwayat pesan (0-7 hari)")
@app_commands.checks.has_permissions(ban_members=True)
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ Gak bisa ban diri sendiri bro.", ephemeral=True)
        return
    if member.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ Posisi role member lebih tinggi/sama dengan bot.", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        await member.ban(reason=reason, delete_message_days=max(0, min(7, delete_days)))
        add_infraction(interaction.guild.id, member.id, interaction.user.id, "BAN", reason)
        embed = discord.Embed(
            title="🔨 Ban",
            description=f"**{member.mention}** di-ban.\n📝 **Alasan:** {reason}",
            color=0xFF0000,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Moderator: {interaction.user.name}")
        await interaction.followup.send(embed=embed)
        await _send_mod_log(interaction.guild, embed)
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await interaction.followup.send(f"❌ Gagal ban: {str(e)[:80]}", ephemeral=True)

@tree.command(name="sunban", description="Unban user by ID (Admin)")
@app_commands.describe(user_id="ID user yang mau di-unban", reason="Alasan unban")
@app_commands.checks.has_permissions(ban_members=True)
async def slash_unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        bans = [b async for b in interaction.guild.bans()]
        target_entry = next((b for b in bans if b.user.id == int(user_id)), None)
        if not target_entry:
            await interaction.followup.send("❌ User gak ada di daftar ban.", ephemeral=True)
            return
        await interaction.guild.unban(target_entry.user, reason=reason)
        add_infraction(interaction.guild.id, int(user_id), interaction.user.id, "UNBAN", reason)
        uname = target_entry.user.name if hasattr(target_entry.user, "name") else str(target_entry.user)
        embed = discord.Embed(
            title="🔓 Unban",
            description=f"**{uname}** (`{user_id}`) di-unban.\n📝 **Alasan:** {reason}",
            color=0x2ECC71,
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"Moderator: {interaction.user.name}")
        await interaction.followup.send(embed=embed)
    except ValueError:
        await interaction.followup.send("❌ user_id harus angka snowflake Discord.", ephemeral=True)
    except Exception as e:
        logger.error(f"Unban error: {e}")
        await interaction.followup.send(f"❌ Gagal unban: {str(e)[:80]}", ephemeral=True)

@tree.command(name="sannounce", description="Kirim pengumuman resmi ke channel (Admin)")
@app_commands.describe(
    title="Judul pengumuman",
    message="Isi pengumuman",
    role="Role yang mau di-mention (opsional)",
    pin="Pin pesan pengumuman?"
)
@app_commands.checks.has_permissions(administrator=True)
async def slash_announce(
    interaction: discord.Interaction,
    title: str,
    message: str,
    role: discord.Role | None = None,
    pin: bool = True
):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return

    await interaction.response.defer()

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("❌ Command ini cuma bisa dipakai di text channel biasa.", ephemeral=True)
        return

    mention = role.mention if role else None
    success, msg = await send_announcement(channel, title, message, mention_role=mention, pin=pin)

    embed = discord.Embed(
        title="📢 Announcement Posted" if success else "❌ Gagal Kirim",
        description=msg,
        color=0x2ECC71 if success else 0xFF0000,
        timestamp=datetime.now()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

def build_help_embed() -> discord.Embed:
    """Single source of truth for the help menu (prefix + slash)."""
    embed = discord.Embed(
        title="📚 Seraphine AI - Daftar Command",
        color=0x7289DA,
        timestamp=datetime.now()
    )
    embed.add_field(
        name="💬 AI Chat",
        value="`!<pertanyaan>` - Tanya apa aja\n`@Seraphine AI <pertanyaan>` - Mention bot",
        inline=False
    )
    embed.add_field(
        name="🎵 Musik (Slash)",
        value="`/splay` - Putar dari YouTube\n`/squeue` - Lihat antrean\n`/sskip` - Lewati lagu\n"
              "`/spause` - Pause\n`/sresume` - Resume\n`/sloop` - Loop lagu\n`/sshuffle` - Acak antrean\n"
              "`/snowplaying` - Lagu yang lagi diputar\n`/sstop` - Stop & keluar VC",
        inline=False
    )
    embed.add_field(
        name="🔨 Moderasi (Admin/Mod)",
        value="`/skick @member` - Kick member\n`/sinfractions @member` - Riwayat pelanggaran\n"
              "`/vmute` `/vunmute` - Mute voice\n`/sannounce` - Kirim pengumuman",
        inline=False
    )
    embed.add_field(
        name="🖥️ Server Info (Admin)",
        value="`!server-info` - Info server\n`!member-list` - Top 20 members\n"
              "`!channel-list` - Daftar channel\n`!role-list` - Daftar role",
        inline=False
    )
    embed.add_field(
        name="ℹ️ Bot Info",
        value=f"Dibuat oleh: **Notzee**\nNama: **Seraphine AI**\nModel: **{AI_MODEL}**",
        inline=False
    )
    embed.set_footer(text="Ketik / untuk lihat semua slash command")
    return embed

@tree.command(name="shelp", description="Lihat daftar command Seraphine AI")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

# ============================================================
#  MUSIC SLASH COMMANDS
# ============================================================

@tree.command(name="splay", description="Putar musik dari YouTube (Seraphine)")
@app_commands.describe(query="Judul lagu atau URL YouTube")
async def slash_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    cfg = load_bot_config()
    if not cfg.get("music_enabled", True):
        await interaction.followup.send("❌ Fitur Musik sedang dinonaktifkan via Dashboard bro!", ephemeral=True)
        return

    if not interaction.guild:
        await interaction.followup.send("❌ Perintah ini cuma bisa dipakai di server Discord bro!", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not member.voice.channel:
        await interaction.followup.send("❌ Kamu harus join voice channel dulu bro!", ephemeral=True)
        return

    try:
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_connected():
            if voice_client.channel != member.voice.channel:
                await voice_client.move_to(member.voice.channel)
        else:
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except:
                    pass
            voice_client = await member.voice.channel.connect()

        player = await YTDLSource.from_url(query, loop=client.loop, stream=True)
        
        if not voice_client.is_playing():
            voice_client._last_source = player
            voice_client.play(player, after=lambda e: play_next(interaction.guild.id, voice_client, interaction.channel))
            embed = discord.Embed(
                title="🎵 Sekarang Diputar",
                description=f"[{player.title}]({player.url})",
                color=0x7289da
            )
            view = MusicControlView(interaction.guild.id)
            try:
                await interaction.followup.send(embed=embed, view=view)
            except:
                await interaction.channel.send(embed=embed, view=view)
        else:
            music_queues[interaction.guild.id].append(player)
            msg = f"✅ Menambahkan ke antrean: **{player.title}** (Urutan ke-{len(music_queues[interaction.guild.id])})"
            try:
                await interaction.followup.send(msg)
            except:
                await interaction.channel.send(msg)

    except Exception as e:
        logger.error(f"Music Error: {e}\n{traceback.format_exc()}")
        try:
            if interaction.guild.voice_client and interaction.guild.voice_client.is_connected():
                await interaction.guild.voice_client.disconnect(force=True)
        except:
            pass

        err_msg = _translate_music_error(e)
        try:
            await interaction.followup.send(err_msg)
        except:
            try:
                await interaction.channel.send(err_msg)
            except:
                pass

@tree.command(name="squeue", description="Lihat antrean musik (Seraphine)")
async def slash_queue(interaction: discord.Interaction):
    queue = music_queues.get(interaction.guild.id, [])
    if not queue:
        await interaction.response.send_message("📋 Antrean musik kosong bro.", ephemeral=True)
        return
    
    queue_list = "\n".join([f"`{i+1}.` [{p.title}]({p.url})" for i, p in enumerate(queue[:15])])
    embed = discord.Embed(
        title="📋 Antrean Musik (Music Queue)",
        description=queue_list,
        color=0x7289da,
        timestamp=datetime.now()
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="sskip", description="Lewati lagu yang sedang diputar (Seraphine)")
async def slash_skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("⏭️ Lagu di-skip!")
    else:
        await interaction.response.send_message("❌ Gak ada lagu yang lagi diputar bro.", ephemeral=True)

# ============================================================
#  MUSIC STATE COMMANDS (pause / resume / nowplaying)
# ============================================================

@tree.command(name="spause", description="Pause lagu yang sedang diputar (Seraphine)")
async def slash_pause(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Musik di-pause.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Gak ada lagu yang lagi diputar bro.", ephemeral=True)

@tree.command(name="sresume", description="Lanjutkan lagu yang di-pause (Seraphine)")
async def slash_resume(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Musik dilanjutkan.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Gak ada musik yang lagi di-pause.", ephemeral=True)

@tree.command(name="snowplaying", description="Lihat lagu yang lagi diputar (Seraphine)")
async def slash_nowplaying(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing() and getattr(vc, "source", None) is not None:
        src = vc.source
        title = getattr(src, "title", None) or "Lagu saat ini"
        url = getattr(src, "url", None) or ""
        embed = discord.Embed(
            title="🎶 Sekarang Diputar",
            description=f"[{title}]({url})" if url else title,
            color=0x7289DA
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Gak ada lagu yang lagi diputar bro.", ephemeral=True)

@tree.command(name="sstop", description="Stop musik & keluar voice channel (Seraphine)")
async def slash_stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        music_queues[interaction.guild.id] = []
        music_loop[interaction.guild.id] = False
        await interaction.response.send_message("🛑 Musik dihentikan dan bot disconnect.")
    else:
        await interaction.response.send_message("❌ Bot lagi gak ada di voice channel.", ephemeral=True)

@tree.command(name="sloop", description="Toggle loop lagu yang lagi diputar (Seraphine)")
async def slash_loop(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("❌ Gak ada lagu yang lagi diputar bro.", ephemeral=True)
        return
    music_loop[interaction.guild.id] = not music_loop[interaction.guild.id]
    state = "AKTIF 🔁" if music_loop[interaction.guild.id] else "MATI ➡️"
    await interaction.response.send_message(f"🔁 Loop lagu sekarang **{state}**.", ephemeral=True)

@tree.command(name="sshuffle", description="Acak urutan antrean musik (Seraphine)")
async def slash_shuffle(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Command ini cuma bisa dipakai di server bro!", ephemeral=True)
        return
    queue = music_queues.get(interaction.guild.id, [])
    if len(queue) < 2:
        await interaction.response.send_message("❌ Antrean kurang dari 2 lagu, gak ada yang diacak.", ephemeral=True)
        return
    random.shuffle(queue)
    await interaction.response.send_message(f"🔀 Antrean diacak! Total {len(queue)} lagu.", ephemeral=True)

# ============================================================
#  DATABASE FUNCTIONS
# ============================================================

def init_db():
    """Initialize database."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        # Conversation table
        c.execute('''CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_message TEXT NOT NULL,
            bot_response TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Infraction table (warn, kick, ban, etc)
        c.execute('''CREATE TABLE IF NOT EXISTS infractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

# Initialize DB on module load so dashboard/Railway imports create tables automatically
init_db()

def save_conversation(user_id: int, user_msg: str, bot_response: str):
    """Save conversation to database."""
    try:
        init_db() # Ensure db table exists
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('INSERT INTO conversation (user_id, user_message, bot_response) VALUES (?, ?, ?)',
                  (user_id, user_msg, bot_response))
        conn.commit()
        
        # Clean up old messages (keep only MAX_DB_MESSAGES per user)
        c.execute('''DELETE FROM conversation WHERE id IN (
            SELECT id FROM conversation WHERE user_id = ? 
            ORDER BY id DESC LIMIT -1 OFFSET ?
        )''', (user_id, MAX_DB_MESSAGES))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving conversation: {e}")

def add_infraction(guild_id: int, user_id: int, moderator_id: int, action_type: str, reason: str):
    """Add infraction record (warn, kick, ban, etc)."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''INSERT INTO infractions (guild_id, user_id, moderator_id, action_type, reason) 
                     VALUES (?, ?, ?, ?, ?)''',
                  (guild_id, user_id, moderator_id, action_type, reason))
        conn.commit()
        conn.close()
        logger.info(f"Infraction added: {action_type} for user {user_id}")
    except Exception as e:
        logger.error(f"Error adding infraction: {e}")

def get_user_infractions(guild_id: int, user_id: int) -> list:
    """Get all infractions for a user in a guild."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''SELECT action_type, reason, timestamp, moderator_id FROM infractions 
                     WHERE guild_id = ? AND user_id = ? 
                     ORDER BY timestamp DESC''',
                  (guild_id, user_id))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Error getting infractions: {e}")
        return []

def get_user_history(user_id: int, limit: int = MAX_HISTORY_MESSAGES) -> str:
    """Get user conversation history (optimized)."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        # Fetch exact limit needed, not more
        c.execute('''SELECT user_message, bot_response FROM conversation 
                     WHERE user_id = ? ORDER BY id DESC LIMIT ?''',
                  (user_id, limit))
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        history = []
        for user_msg, bot_resp in reversed(rows):
            history.append(f"User: {user_msg}\nBot: {bot_resp}")
        
        return "\n\n".join(history)
    except Exception as e:
        logger.error(f"Error getting history: {e}")
        try:
            init_db()
        except:
            pass
        return ""

# ============================================================
#  NEWS FUNCTIONS
# ============================================================

def fetch_trending_news() -> str:
    """Fetch trending news from NewsAPI with fallback."""
    try:
        url = f"{NEWSAPI_BASE_URL}/everything"
        params = {
            "q": "Indonesia OR viral OR trending",
            "sortBy": "publishedAt",
            "language": "id",
            "pageSize": 5,
            "apiKey": NEWSAPI_KEY
        }
        
        logger.info("Fetching trending news...")
        res = requests.get(url, params=params, timeout=10)
        hasil = res.json()
        
        if hasil.get("status") != "ok":
            error_msg = hasil.get("message", "Unknown error")
            logger.warning(f"NewsAPI error: {error_msg}")
            return f"⚠️ Gak bisa fetch berita: {error_msg}"
        
        articles = hasil.get("articles", [])
        if not articles:
            return "⚠️ Gak ada berita trending saat ini 😅"
        
        berita_text = "🔥 **Berita Trending Hari Ini:**\n\n"
        for i, article in enumerate(articles, 1):
            title = article.get("title", "No title")
            desc = article.get("description", "No description")
            source = article.get("source", {}).get("name", "Unknown")
            
            if desc and len(desc) > 120:
                desc = desc[:120] + "..."
            
            berita_text += f"**{i}. {title}**\n"
            berita_text += f"   {desc}\n"
            berita_text += f"   *Sumber: {source}*\n\n"
        
        logger.info("News fetched successfully")
        return berita_text
        
    except requests.exceptions.Timeout:
        logger.warning("News API timeout")
        return "⚠️ Timeout fetch berita, coba lagi nanti"
    except Exception as e:
        logger.error(f"Error fetching news: {e}")
        return f"⚠️ Error fetch berita: {str(e)[:50]}"

# ============================================================
#  AI FUNCTIONS (MERGED & OPTIMIZED)
# ============================================================

async def tanya_ai(pertanyaan: str, user_id: int, user_name: str, include_trending: bool = False) -> str:
    """
    Send question to OpenRouter with optional trending news context.
    Merged function replacing both tanya_ai and tanya_ai_dengan_trending.
    """
    try:
        # Build context
        context_parts = [KEPRIBADIAN]
        
        if include_trending:
            # Jalankan di thread terpisah supaya event loop tetap responsif
            berita = await asyncio.to_thread(fetch_trending_news)
            context_parts.append(f"Berita Trending Saat Ini:\n{berita}")
        
        history = get_user_history(user_id)
        if history:
            context_parts.append(f"Recent context:\n{history}")
        
        context_parts.append(f"User {user_name} bertanya: {pertanyaan}")
        full_prompt = "\n\n".join(context_parts)
        
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://discord.com",
            "X-Title": "Seraphine AI Bot"
        }
        
        data = {
            "model": AI_MODEL,
            "messages": [{"role": "user", "content": full_prompt}],
            "temperature": 0.7,
            "max_tokens": 1500
        }
        
        logger.info(f"Requesting AI response for user {user_id}")
        # requests.post sinkron nge-block event loop Discord -> bot freeze.
        # Jalankan di thread terpisah biar bot tetap responsif.
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    requests.post,
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    json=data,
                    headers=headers,
                    timeout=45
                ),
                timeout=50
            )
        except asyncio.TimeoutError:
            logger.warning("OpenRouter timeout (async guard)")
            return "⏱️ AI sedang load, coba lagi dalam beberapa detik"
        hasil = res.json()
        
        if "error" in hasil:
            error_msg = hasil["error"].get("message", "Unknown error")
            logger.error(f"OpenRouter error: {error_msg}")
            return f"❌ Duh, AI error: {error_msg[:100]}"
        
        balasan = hasil.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        
        if balasan:
            save_conversation(user_id, pertanyaan, balasan)
            logger.info(f"Response saved for user {user_id}")
            return balasan
        else:
            return "❌ Hmm, gua gabisa jawab pertanyaan itu 😅"
        
    except requests.exceptions.Timeout:
        logger.warning("OpenRouter timeout")
        return "⏱️ AI sedang load, coba lagi dalam beberapa detik"
    except requests.exceptions.ConnectionError:
        logger.error("Connection error to OpenRouter")
        return "🌐 Internet error, coba lagi nanti"
    except Exception as e:
        logger.error(f"Unexpected error in tanya_ai: {e}")
        return f"❌ Error: {str(e)[:50]}"

# ============================================================
#  FORMATTING FUNCTIONS
# ============================================================

def create_response_embed(title: str, description: str, color: int = 0x7289da) -> discord.Embed:
    """Create a formatted embed response."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now()
    )
    return embed

def truncate_response(text: str, limit: int = RESPONSE_CHAR_LIMIT) -> str:
    """Truncate response if too long."""
    if len(text) > limit:
        return text[:limit-20] + "\n\n*... (pesan kepanjangan)*"
    return text

# ============================================================
#  MODERATION FUNCTIONS
# ============================================================

async def kick_user(member: discord.Member, reason: str = "No reason provided", moderator_id: int = None) -> tuple[bool, str]:
    """Kick a user from server and log infraction."""
    try:
        if member.bot:
            return False, "❌ Tidak bisa kick bot!"
        
        if member.guild_permissions.administrator:
            return False, "❌ Tidak bisa kick admin!"
        
        await member.kick(reason=reason)
        
        # Log infraction
        if moderator_id:
            add_infraction(member.guild.id, member.id, moderator_id, "KICK", reason)
        
        logger.info(f"Kicked user {member.name} for reason: {reason}")
        return True, f"✅ User {member.name} berhasil di-kick\nAlasan: {reason}"
    except discord.Forbidden:
        return False, "❌ Bot tidak punya permission untuk kick user ini"
    except Exception as e:
        logger.error(f"Error kicking user: {e}")
        return False, f"❌ Error kick user: {str(e)[:50]}"

async def send_announcement(channel: discord.TextChannel, title: str, message: str,
                           mention_role: str | None = None, pin: bool = True) -> tuple[bool, str]:
    """Send announcement to channel."""
    try:
        mention_text = ""
        if mention_role:
            mention_text = f"{mention_role}\n"
        
        embed = discord.Embed(
            title=f"📢 {title}",
            description=message,
            color=0xFF5733,
            timestamp=datetime.now()
        )
        embed.set_footer(text="Official Announcement")
        
        msg = await channel.send(f"{mention_text}", embed=embed)
        
        if pin:
            await msg.pin()
        
        logger.info(f"Announcement sent to {channel.name}")
        return True, f"✅ Announcement sent ke channel {channel.mention}"
    except discord.Forbidden:
        return False, "❌ Bot tidak punya permission di channel ini"
    except Exception as e:
        logger.error(f"Error sending announcement: {e}")
        return False, f"❌ Error send announcement: {str(e)[:50]}"

# ============================================================
#  PERMISSION CHECKS
# ============================================================

def has_server_permission(user: discord.User, guild: discord.Guild) -> bool:
    """Check if user has permission to run server commands."""
    try:
        member = guild.get_member(user.id)
        if member is None:
            logger.warning(f"Could not find member {user.id} in guild {guild.id}")
            return False
        
        # Check owner
        if user.id == guild.owner_id:
            logger.info(f"User {user.name} is server owner")
            return True
        
        # Check admin permission
        if member.guild_permissions.administrator:
            logger.info(f"User {user.name} has admin permission")
            return True
        
        logger.warning(f"User {user.name} does not have admin permission")
        return False
    except Exception as e:
        logger.error(f"Error checking permissions for {user.id}: {e}")
        return False

def is_moderator(member: discord.Member) -> bool:
    """Check if member is moderator (admin or has Moderator role)."""
    if member.guild_permissions.administrator or member.guild_permissions.moderate_members:
        return True
    
    # Check if has Moderator role
    for role in member.roles:
        if role.name.lower() in ["moderator", "mod", "admin"]:
            return True
    
    return False

# ============================================================
#  DISCORD EVENTS
# ============================================================

@client.event
async def on_ready():
    logger.info("=" * 50)
    logger.info(f"✅ BOT ONLINE: {client.user}")
    logger.info("Bot siap diajak ngobrol!")
    logger.info("=" * 50)
    
    try:
        await tree.sync()
        for guild in client.guilds:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        logger.info("✅ Slash commands synchronized successfully (Global & Guilds)")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    
    # Set status
    await client.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/shelp")
    )

@client.event
async def on_voice_state_update(member, before, after):
    cfg = load_bot_config()
    if not cfg.get("voice_log_enabled", True):
        return

    if before.channel != after.channel:
        try:
            mod_channel = await get_or_create_mod_log_channel(member.guild)
            if not mod_channel:
                return

            if before.channel is None and after.channel is not None:
                # Joined
                embed = discord.Embed(
                    title="🎙️ Member Joined Voice",
                    color=0x2ECC71,
                    timestamp=datetime.now()
                )
                embed.add_field(name="👤 Member", value=f"{member.mention} ({member.name})", inline=False)
                embed.add_field(name="📁 Channel", value=after.channel.name, inline=False)
                await mod_channel.send(embed=embed)

            elif before.channel is not None and after.channel is None:
                # Left
                embed = discord.Embed(
                    title="🎙️ Member Left Voice",
                    color=0xE74C3C,
                    timestamp=datetime.now()
                )
                embed.add_field(name="👤 Member", value=f"{member.mention} ({member.name})", inline=False)
                embed.add_field(name="📁 Channel", value=before.channel.name, inline=False)
                await mod_channel.send(embed=embed)

            elif before.channel is not None and after.channel is not None:
                # Moved
                moderator = None
                async for entry in member.guild.audit_logs(action=discord.AuditLogAction.member_move, limit=1):
                    if entry.target and hasattr(entry.target, 'id') and entry.target.id == member.id and (datetime.now() - entry.created_at.replace(tzinfo=None)).total_seconds() < 5:
                        moderator = entry.user
                        break
                
                embed = discord.Embed(
                    title="🎙️ Member Moved in Voice",
                    color=0x3498DB,
                    timestamp=datetime.now()
                )
                embed.add_field(name="👤 Member", value=f"{member.mention} ({member.name})", inline=False)
                embed.add_field(name="📁 From", value=before.channel.name, inline=True)
                embed.add_field(name="📁 To", value=after.channel.name, inline=True)
                if moderator:
                    embed.add_field(name="🛡️ Moved By", value=f"{moderator.mention} ({moderator.name})", inline=False)
                else:
                    embed.add_field(name="🛡️ Moved By", value="Self", inline=False)
                
                await mod_channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in on_voice_state_update: {e}")

@client.event
async def on_message(pesan):
    if pesan.author.bot:
        return
    
    cfg = load_bot_config()

    # ============================================================
    #  TOXIC MESSAGE AUTO-DELETE (SEMUA MESSAGE)
    # ============================================================
    if cfg.get("automod_enabled", True) and contains_toxic(pesan.content):
        logger.warning(f"Toxic message detected from {pesan.author.name}: {pesan.content[:50]}")
        
        try:
            # Log ke mod channel
            await log_toxic_message(pesan.guild, pesan.author, pesan.content, pesan.channel.name)
            
            # Delete message
            await pesan.delete()
            
            # DM user
            embed = discord.Embed(
                title="⚠️ Pesan Dihapus",
                description=f"Halo {pesan.author.name}, pesan mu mengandung kata-kata yang tidak sopan.\nMohon jaga bahasa yang baik di server ini 😊",
                color=0xFF5733,
                timestamp=datetime.now()
            )
            
            await pesan.author.send(embed=embed)
            logger.info(f"Toxic message deleted and user notified")
        except discord.Forbidden:
            logger.warning("Cannot delete toxic message or DM user (permission denied)")
            try:
                await pesan.reply("⚠️ Pesan mu mengandung kata-kata tidak sopan. Mohon jaga bahasa yang baik!", delete_after=5)
            except:
                pass
        except Exception as e:
            logger.error(f"Error handling toxic message: {e}")
        
        return
    
    # ============================================================
    #  ANTI-SPAM: flood / invite / mass-mention
    # ============================================================
    if cfg.get("automod_enabled", True) and pesan.guild and not is_moderator(pesan.author):
        violation = None
        if _INVITE_RE.search(pesan.content):
            violation = "invite link"
        else:
            violation = _register_and_check_spam(
                pesan.author.id, pesan.content, len(pesan.mentions), pesan.channel.id
            )
        if violation:
            await _handle_spam_violation(pesan, violation)
            return

    # ============================================================
    #  RATE LIMIT CHECK
    # ============================================================
    if not check_rate_limit(pesan.author.id):
        logger.warning(f"Rate limit hit for user {pesan.author.id}")
        return
    
    isi = pesan.content.strip()
    pertanyaan = None
    command = None
    
    # Parse command atau mention
    if isi.startswith(PREFIX):
        pertanyaan = isi[len(PREFIX):].strip()
        if pertanyaan:
            command = pertanyaan.split()[0].lower()
    elif client.user in pesan.mentions:
        pertanyaan = isi.replace(f"<@{client.user.id}>", "").strip()
    
    if not pertanyaan:
        return
    
    logger.info(f"Message from {pesan.author.name}: {pertanyaan[:50]}")
    
    # ============================================================
    #  HELP COMMAND
    # ============================================================
    if command == "help":
        await pesan.reply(embed=build_help_embed())
        return
    
    # ============================================================
    #  TRENDING COMMAND
    # ============================================================
    if command == "trending":
        async with pesan.channel.typing():
            jawaban = await tanya_ai(
                "Apa yang trending hari ini? Berikan penjelasan singkat tentang trending topics terkini.",
                pesan.author.id,
                pesan.author.name,
                include_trending=True
            )
        
        jawaban = truncate_response(jawaban)
        embed = create_response_embed("🔥 Trending Hari Ini", jawaban)
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  SERVER INFO COMMAND
    # ============================================================
    if command == "server-info":
        if not has_server_permission(pesan.author, pesan.guild):
            await pesan.reply("❌ Hanya admin yang bisa pakai command ini")
            return
        
        guild = pesan.guild
        online = sum(1 for m in guild.members if m.status != discord.Status.offline)
        offline = guild.member_count - online
        
        embed = discord.Embed(
            title=f"🖥️ Info Server: {guild.name}",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        
        embed.add_field(name="👥 Total Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="🟢 Online", value=str(online), inline=True)
        embed.add_field(name="⚫ Offline", value=str(offline), inline=True)
        embed.add_field(name="📅 Created", value=guild.created_at.strftime("%d %b %Y"), inline=False)
        embed.add_field(name="👑 Owner", value=f"<@{guild.owner_id}>", inline=False)
        embed.add_field(name="📋 Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="💬 Channels", value=str(len(guild.channels)), inline=True)
        
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  MEMBER LIST COMMAND
    # ============================================================
    if command == "member-list":
        if not has_server_permission(pesan.author, pesan.guild):
            await pesan.reply("❌ Hanya admin yang bisa pakai command ini")
            return
        
        guild = pesan.guild
        members = "\n".join([f"• {m.name}#{m.discriminator}" for m in guild.members[:20]])
        
        embed = discord.Embed(
            title="👥 Top 20 Members",
            description=members,
            color=0x7289da,
            timestamp=datetime.now()
        )
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  CHANNEL LIST COMMAND
    # ============================================================
    if command == "channel-list":
        if not has_server_permission(pesan.author, pesan.guild):
            await pesan.reply("❌ Hanya admin yang bisa pakai command ini")
            return
        
        guild = pesan.guild
        channels = "\n".join([f"• #{c.name}" for c in guild.channels[:15]])
        
        embed = discord.Embed(
            title="💬 Daftar Channels",
            description=channels,
            color=0x7289da,
            timestamp=datetime.now()
        )
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  ROLE LIST COMMAND
    # ============================================================
    if command == "role-list":
        if not has_server_permission(pesan.author, pesan.guild):
            await pesan.reply("❌ Hanya admin yang bisa pakai command ini")
            return
        
        guild = pesan.guild
        roles = "\n".join([f"• @{r.name}" for r in guild.roles[:15]])
        
        embed = discord.Embed(
            title="📋 Daftar Roles",
            description=roles,
            color=0x7289da,
            timestamp=datetime.now()
        )
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  PREFIX REDIRECT: musik & moderasi sekarang slash-only
    # ============================================================
    if command in ["play", "splay", "queue", "squeue", "skip", "sskip", "stop", "sstop", "pause", "spause", "resume", "sresume", "nowplaying", "snowplaying", "loop", "sloop", "shuffle", "sshuffle"]:
        await pesan.reply("🎵 Command musik sekarang pakai **Slash Command (`/`)** khusus Seraphine bro! Coba ketik `/splay`, `/squeue`, `/sskip`, atau `/sstop` 😉")
        return

    if command in ["kick", "infractions", "announce"]:
        await pesan.reply("🔨 Command moderasi sekarang pakai **Slash Command (`/`)** bro! Coba ketik `/skick`, `/sinfractions`, atau `/sannounce` 😉")
        return

    # ============================================================
    #  DEFAULT AI CHAT
    # ============================================================
    if not cfg.get("ai_chat_enabled", True):
        return

    async with pesan.channel.typing():
        jawaban = await tanya_ai(pertanyaan, pesan.author.id, pesan.author.name, include_trending=False)
    
    jawaban = truncate_response(jawaban)
    
    # Cek apakah response berisi code block
    if "```" in jawaban:
        embed = create_response_embed("💬 Jawaban", jawaban)
    else:
        embed = create_response_embed("💬 Jawaban", jawaban)
    
    await pesan.reply(embed=embed)

# ============================================================
#  STARTUP
# ============================================================

if __name__ == "__main__":
    # Validate environment variables
    if not DISCORD_TOKEN or not OPENROUTER_API_KEY or not NEWSAPI_KEY:
        logger.error("❌ Missing environment variables! Check .env file")
        exit(1)
    
    logger.info("Starting bot...")
    init_db()
    
    try:
        client.run(DISCORD_TOKEN)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        exit(1)
