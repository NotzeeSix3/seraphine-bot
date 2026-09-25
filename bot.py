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
import json
import os
import random
import re
import ssl
import tempfile
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
from better_profanity.better_profanity import read_wordlist as _bp_read_wordlist

from cortex import cortex_handle

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

# Folder temp buat hasil download audio (dibersihkan tiap selesai diputar)
_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "seraphine_music")
try:
    os.makedirs(_DOWNLOAD_DIR, exist_ok=True)
except Exception as _e:
    _DOWNLOAD_DIR = tempfile.gettempdir()
    ytdl_log.warning(f"Gagal bikin folder download, pakai tmp: {_e}")

def _make_ytdl(player_clients=None):
    cookies_file = _get_cookies_file()
    opts = {
        # Format audio paling kompatibel. Fallback 'best' supaya tidak
        # menghasilkan file '.NA' di container tanpa postprocessor.
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': os.path.join(_DOWNLOAD_DIR, '%(id)s.%(ext)s'),
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'nopart': False,
        'retries': 3,
        'fragment_retries': 3,
    }
    if player_clients:
        opts['extractor_args'] = {'youtube': {'player_client': player_clients}}
    if cookies_file:
        opts['cookiefile'] = cookies_file
    return yt_dlp.YoutubeDL(opts)

ytdl = _make_ytdl()

# --- Diagnostik runtime YouTube (Deno/EJS) — muncul di log Railway saat startup ---
def _log_yt_diag():
    import shutil as _sh, os as _os
    deno = _sh.which("deno") or _os.path.exists("/usr/local/bin/deno")
    ffmpeg = _sh.which("ffmpeg")
    ytdl_log.info(f"[yt-diag] deno={deno} ffmpeg={ffmpeg} cookies={bool(COOKIES_FILE)}")
    try:
        import yt_dlp_ejs
        ytdl_log.info(f"[yt-diag] yt-dlp-ejs OK v{getattr(yt_dlp_ejs, '__version__', '?')}")
    except Exception as e:
        ytdl_log.warning(f"[yt-diag] yt-dlp-ejs TIDAK ADA: {e}")
    try:
        from yt_dlp.globals import supported_js_runtimes
        ytdl_log.info(f"[yt-diag] js_runtimes={list(supported_js_runtimes.value)}")
    except Exception as e:
        ytdl_log.warning(f"[yt-diag] js_runtimes info gagal: {e}")

try:
    _log_yt_diag()
except Exception as _e:
    pass

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
    None,  # default (web) — jangan [None]! player_client=[None] bikin
           # yt-dlp crash 'NoneType' object has no attribute 'lower'
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
    attempts = list(_YTDL_FALLBACK_CLIENTS)
    last_err = None
    for clients in attempts:
        if clients is None:
            extractor = ytdl
        else:
            # Sanitasi: buang entri None di dalam list player_client,
            # karena yt-dlp crash ('NoneType'.lower()) kalau dapat None.
            clients = [c for c in clients if c] or None
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

def _get_yt_oembed_title(yt_url):
    """Ambil judul video YouTube via oEmbed publik (tidak pernah kena bot-check)."""
    import urllib.request, urllib.parse, json
    try:
        api_url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(yt_url, safe=':/?=')}&format=json"
        req = urllib.request.Request(api_url, headers={'User-Agent': _FFMPEG_UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            res = json.loads(r.read().decode('utf-8'))
            return res.get('title')
    except Exception as e:
        ytdl_log.warning(f"oEmbed YouTube gagal untuk {yt_url}: {e}")
        return None


def _clean_track_title(title):
    """Bersihkan noise umum di judul YouTube video supaya pencarian audio akurat."""
    if not title:
        return ""
    cleaned = re.sub(r'\([^)]*(?:official|video|audio|lyric|visualizer|mv|hd|4k|clip)[^)]*\)', '', title, flags=re.I)
    cleaned = re.sub(r'\[[^\]]*(?:official|video|audio|lyric|visualizer|mv|hd|4k|clip)[^\]]*\]', '', cleaned, flags=re.I)
    cleaned = re.sub(r'\s*\|\s*.*$', '', cleaned)
    return cleaned.strip()


def _extract_from_soundcloud(query, download=True):
    """Ekstraksi fallback audio via SoundCloud (bebas bot-check & aman di IP datacenter)."""
    sc_query = f"scsearch1:{query}"
    ytdl_log.info(f"[music-fallback] Mencoba SoundCloud search: {sc_query}")
    try:
        data = ytdl.extract_info(sc_query, download=download)
        if data and data.get('entries'):
            data = data['entries'][0]
        if not data:
            return None, None
        fn = None
        if download:
            fn = (data.get('filepath')
                  or data.get('_filename')
                  or ytdl.prepare_filename(data))
            if fn and os.path.exists(fn):
                return data, fn
        if data.get('url'):
            return data, None
    except Exception as e:
        ytdl_log.warning(f"[music-fallback] SoundCloud gagal ({query}): {e}")
    return None, None


def _build_ffmpeg_options(data=None):
    """Bangun opsi ffmpeg. Kalau data punya http_headers dari yt-dlp,
    pakai header itu (Cookie/Referer/UA) supaya googlevideo.com tidak 403."""
    hdrs = (data or {}).get('http_headers') or {}
    before = ('-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5')
    # User-Agent
    ua = hdrs.get('User-Agent') or _FFMPEG_UA
    before += f' -user_agent "{ua}"'
    # Header tambahan yang sering wajib buat googlevideo / stream
    extra = []
    extractor = ((data or {}).get('extractor') or '').lower()
    if hdrs.get('Referer'):
        extra.append(f'Referer: {hdrs["Referer"]}')
    elif 'soundcloud' in extractor:
        extra.append('Referer: https://soundcloud.com/')
    else:
        extra.append('Referer: https://www.youtube.com/')
    if hdrs.get('Cookie'):
        extra.append(f'Cookie: {hdrs["Cookie"]}')
    if hdrs.get('Origin'):
        extra.append(f'Origin: {hdrs["Origin"]}')
    extra.append('Accept: */*')
    extra.append('Accept-Language: en-US,en;q=0.9')
    if extra:
        joined = "\
\\n".join(extra) + "\
\\n"
        before += f' -headers "{joined}"'
    return {'options': '-vn', 'before_options': before}


ffmpeg_options = _build_ffmpeg_options()

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url') or data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()

        def _resolve():
            """Resolve ke audio source. Prioritas: YouTube -> Auto-Fallback: SoundCloud."""
            nonlocal url
            is_direct_url = url.startswith(('http://', 'https://', 'www.'))
            if not is_direct_url and not url.startswith(('ytsearch', 'scsearch')):
                yt_query = f"ytsearch1:{url}"
            else:
                yt_query = url

            yt_err = None
            # 1) Coba YouTube dulu jika bukan link SoundCloud eksplisit
            if 'soundcloud.com' not in yt_query and not yt_query.startswith('scsearch'):
                try:
                    data = _extract_info_with_fallback(yt_query, download=True)
                    if data and data.get('entries'):
                        data = data['entries'][0]
                    if data:
                        fn = (data.get('filepath')
                              or data.get('_filename')
                              or ytdl.prepare_filename(data))
                        if fn and os.path.exists(fn):
                            return data, fn
                except Exception as e:
                    yt_err = e
                    ytdl_log.warning(f"YouTube download=True gagal ({yt_query}): {e}")

                try:
                    data = _extract_info_with_fallback(yt_query, download=False)
                    if data and data.get('entries'):
                        data = data['entries'][0]
                    if data and not data.get('url'):
                        video_url = data.get('webpage_url') or f"https://www.youtube.com/watch?v={data.get('id')}"
                        data = _extract_info_with_fallback(video_url, download=False)
                        if data and data.get('entries'):
                            data = data['entries'][0]
                    if data and data.get('url'):
                        return data, None
                except Exception as e:
                    yt_err = e
                    ytdl_log.warning(f"YouTube download=False gagal ({yt_query}): {e}")

            # 2) AUTO-FALLBACK KE SOUNDCLOUD (Jika YouTube kena bot-check / 403 / error)
            fallback_query = None
            orig_title = None

            if not is_direct_url:
                fallback_query = re.sub(r'^ytsearch\d*:\s*', '', url).strip()
            elif 'youtube.com' in url or 'youtu.be' in url:
                orig_title = _get_yt_oembed_title(url)
                if orig_title:
                    fallback_query = _clean_track_title(orig_title) or orig_title
            elif 'soundcloud.com' in url or url.startswith('scsearch'):
                fallback_query = url

            if fallback_query:
                ytdl_log.info(f"[music-fallback] Menjalankan fallback SoundCloud: '{fallback_query}'")
                if fallback_query.startswith(('http://', 'https://')):
                    try:
                        sc_data = ytdl.extract_info(fallback_query, download=True)
                        if sc_data:
                            fn = sc_data.get('filepath') or sc_data.get('_filename') or ytdl.prepare_filename(sc_data)
                            if fn and os.path.exists(fn):
                                return sc_data, fn
                        sc_data = ytdl.extract_info(fallback_query, download=False)
                        if sc_data and sc_data.get('url'):
                            return sc_data, None
                    except Exception as e:
                        ytdl_log.warning(f"Direct SoundCloud link gagal: {e}")
                else:
                    sc_data, sc_fn = _extract_from_soundcloud(fallback_query, download=True)
                    if not sc_data and not sc_fn:
                        sc_data, sc_fn = _extract_from_soundcloud(fallback_query, download=False)
                    if sc_data:
                        if orig_title and not sc_data.get('title'):
                            sc_data['title'] = orig_title
                        return sc_data, sc_fn

            if yt_err:
                raise yt_err
            raise Exception("Lagu tidak dapat ditemukan di YouTube maupun SoundCloud.")

        data, local_file = await loop.run_in_executor(None, _resolve)

        if not data or not isinstance(data, dict):
            raise Exception("YouTube/SoundCloud extraction kosong / hasil pencarian tidak ditemukan.")

        # Pilih sumber: file lokal (aman) atau stream URL (dengan header lengkap)
        if local_file and os.path.exists(local_file):
            source_path = local_file
            opts = {'options': '-vn'}  # file lokal gak perlu header
        else:
            source_path = data.get('url')
            if not source_path:
                raise Exception("Gagal mendapatkan URL stream / file lagu.")
            opts = _build_ffmpeg_options(data)

        return cls(discord.FFmpegPCMAudio(source_path, **opts), data=data)

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


# ============================================================
#  SPOTIFY SUPPORT (resolve Spotify URL -> YouTube search query)
#  Spotify audio tidak bisa di-stream langsung tanpa premium
#  device, jadi strateginya: ambil metadata lagu via Spotify
#  Web API (Client Credentials Flow, tanpa login user), lalu
#  putar audionya lewat YouTube search (yt-dlp yang sudah ada).
# ============================================================

_SPOTIFY_TOKEN_CACHE = {"token": None, "expires_at": 0.0}

_SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)"
)


def _is_spotify_url(query: str) -> bool:
    return bool(query and "open.spotify.com" in query and _SPOTIFY_URL_RE.search(query))


def _spotify_get_token() -> str | None:
    """Ambil access token Spotify via Client Credentials Flow (cache sampai expired)."""
    import time as _time
    import base64 as _b64
    now = _time.time()
    if _SPOTIFY_TOKEN_CACHE["token"] and now < _SPOTIFY_TOKEN_CACHE["expires_at"] - 60:
        return _SPOTIFY_TOKEN_CACHE["token"]
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        creds = _b64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        _SPOTIFY_TOKEN_CACHE["token"] = payload.get("access_token")
        _SPOTIFY_TOKEN_CACHE["expires_at"] = now + float(payload.get("expires_in", 3600))
        return _SPOTIFY_TOKEN_CACHE["token"]
    except Exception as e:
        ytdl_log.warning(f"Spotify token gagal: {e}")
        return None


def _spotify_api(path: str, params: dict | None = None) -> dict | None:
    token = _spotify_get_token()
    if not token:
        return None
    try:
        r = requests.get(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15,
        )
        if r.status_code == 401:  # token basi, refresh sekali
            _SPOTIFY_TOKEN_CACHE["token"] = None
            token = _spotify_get_token()
            if not token:
                return None
            r = requests.get(
                f"https://api.spotify.com/v1{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=15,
            )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        ytdl_log.warning(f"Spotify API {path} gagal: {e}")
        return None


def _track_to_query(track: dict) -> str | None:
    """Ubah objek track Spotify jadi query 'Artist - Judul' buat YouTube search."""
    if not track:
        return None
    name = (track.get("name") or "").strip()
    artists = ", ".join(a.get("name", "") for a in (track.get("artists") or []) if a.get("name"))
    if not name:
        return None
    return f"{artists} - {name}".strip(" -") if artists else name


def resolve_spotify_query(query: str) -> tuple[list[str], str]:
    """Resolve input Spotify jadi list query YouTube + label sumber.

    Returns: (queries, label). queries = list 'Artist - Judul'.
    Raises Exception dengan pesan user-friendly kalau gagal.
    """
    m = _SPOTIFY_URL_RE.search(query or "")
    if m:
        kind, sid = m.group(1), m.group(2)
        if kind == "track":
            data = _spotify_api(f"/tracks/{sid}")
            q = _track_to_query(data or {})
            if not q:
                raise Exception("Gagal ambil info lagu Spotify (cek SPOTIFY_CLIENT_ID/SECRET).")
            title = (data or {}).get("name", "lagu Spotify")
            return [q], f"Spotify track: {title}"
        if kind == "playlist":
            # Ambil sampai 50 lagu pertama (batas wajar buat antrean bot)
            data = _spotify_api(f"/playlists/{sid}/tracks", {"limit": 50, "fields": "items(track(name,artists(name)))"})
            items = (data or {}).get("items", [])
            queries = [q for t in items if (q := _track_to_query((t or {}).get("track") or {}))]
            if not queries:
                raise Exception("Playlist Spotify kosong / tidak bisa dibaca (pastikan public).")
            pname = (_spotify_api(f"/playlists/{sid}", {"fields": "name"}) or {}).get("name", "playlist")
            return queries, f"Spotify playlist: {pname} ({len(queries)} lagu)"
        if kind == "album":
            data = _spotify_api(f"/albums/{sid}/tracks", {"limit": 50})
            items = (data or {}).get("items", [])
            # Track album tidak bawa full artist, fallback ke nama album artist
            album = _spotify_api(f"/albums/{sid}", {"fields": "name,artists(name)"}) or {}
            artist = ", ".join(a.get("name", "") for a in album.get("artists", []))
            queries = []
            for t in items or []:
                t = t or {}
                nm = (t.get("name") or "").strip()
                if nm:
                    queries.append(f"{artist} - {nm}".strip(" -") if artist else nm)
            if not queries:
                raise Exception("Album Spotify tidak bisa dibaca.")
            return queries, f"Spotify album: {album.get('name', 'album')} ({len(queries)} lagu)"
        raise Exception("Link artist Spotify belum didukung — pakai link track/playlist/album ya.")
    # Bukan URL tapi query 'spotify: ...' -> cari via Spotify Search API
    keyword = re.sub(r"(?i)^\s*spotify\s*:\s*", "", query or "").strip()
    if not keyword:
        raise Exception("Kasih judul lagu setelah 'spotify:' ya, contoh: spotify: rewrite the stars.")
    data = _spotify_api("/search", {"q": keyword, "type": "track", "limit": 5})
    items = ((data or {}).get("tracks") or {}).get("items", [])
    queries = [q for t in items if (q := _track_to_query(t or {}))]
    if not queries:
        raise Exception("Spotify search ga nemu hasil — coba kata kunci lain.")
    return [queries[0]], f"Spotify search: {keyword}"


# ---- Fallback tanpa Web API (buat app Development yang gak punya Premium) ----
# Spotify Web API sekarang butuh akun owner Premium ("Active premium
# subscription required for the owner of the app"). Kalau API diblokir,
# kita baca metadata dari HALAMAN EMBED publik Spotify
# (open.spotify.com/embed/<type>/<id>) yang menyertakan JSON __NEXT_DATA__
# berisi nama track + artist + trackList, TANPA butuh token/Premium.
# Audionya tetap diputar lewat YouTube search (yt-dlp).

_SPOTIFY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _spotify_embed_entity(kind: str, sid: str) -> dict | None:
    """Ambil objek entity dari __NEXT_DATA__ halaman embed publik Spotify."""
    try:
        import json as _json
        r = requests.get(
            f"https://open.spotify.com/embed/{kind}/{sid}",
            headers={"User-Agent": _SPOTIFY_UA},
            timeout=15,
        )
        r.raise_for_status()
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            r.text, re.S,
        )
        if not m:
            return None
        data = _json.loads(m.group(1))
        return (data.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("data", {})
                    .get("entity"))
    except Exception as e:
        ytdl_log.warning(f"Spotify embed gagal ({kind}/{sid}): {e}")
        return None


def _spotify_scrape_track(url: str) -> tuple[str | None, str | None]:
    """Ambil (query 'Artist - Judul', label) dari halaman embed Spotify."""
    m = _SPOTIFY_URL_RE.search(url or "")
    if not m:
        return None, None
    kind, sid = m.group(1), m.group(2)
    ent = _spotify_embed_entity("track" if kind == "track" else kind, sid)
    if not ent:
        return None, None
    name = (ent.get("name") or ent.get("title") or "").strip()
    artists = ", ".join(
        a.get("name", "") for a in (ent.get("artists") or []) if a.get("name")
    )
    if not name:
        return None, None
    q = f"{artists} - {name}".strip(" -") if artists else name
    return q, f"Spotify (embed): {name}"


def _spotify_scrape_list(url: str, max_items: int = 50) -> list[str]:
    """Ambil daftar lagu dari halaman embed album/playlist Spotify."""
    m = _SPOTIFY_URL_RE.search(url or "")
    if not m:
        return []
    kind, sid = m.group(1), m.group(2)
    ent = _spotify_embed_entity(kind, sid)
    if not ent:
        return []
    queries: list[str] = []
    for t in (ent.get("trackList") or []):
        title = (t.get("title") or "").replace("\xa0", " ").strip()
        if not title:
            continue
        sub = (t.get("subtitle") or "").replace("\xa0", " ").strip()  # biasanya nama artist
        q = f"{sub} - {title}".strip(" -") if sub else title
        if q not in queries:
            queries.append(q)
        if len(queries) >= max_items:
            break
    return queries


def resolve_spotify_scrape(query: str) -> tuple[list[str], str]:
    """Fallback resolver tanpa Web API. Raises Exception kalau gagal total."""
    m = _SPOTIFY_URL_RE.search(query or "")
    if m:
        kind, _sid = m.group(1), m.group(2)
        url = "https://open.spotify.com/" + m.group(0).split("open.spotify.com/")[-1]
        if kind == "track":
            q, label = _spotify_scrape_track(url)
            if not q:
                raise Exception(
                    "Gagal baca metadata Spotify. Coba pakai judul lagu / link YouTube."
                )
            return [q], label or "Spotify track (embed)"
        # album / playlist
        queries = _spotify_scrape_list(url)
        if queries:
            return queries, f"Spotify {kind} (embed): {len(queries)} lagu"
        q, label = _spotify_scrape_track(url)
        if q:
            return [q], label or "Spotify track (embed)"
        raise Exception(
            f"Gagal baca {kind} Spotify. Coba share link satu track, "
            "atau pakai judul lagu / YouTube."
        )
    # query 'spotify: ...' -> gak bisa tanpa API, suruh pakai judul biasa
    keyword = re.sub(r"(?i)^\s*spotify\s*:\s*", "", query or "").strip()
    if keyword:
        return [f"{keyword} audio"], "YouTube search (Spotify API nonaktif)"
    raise Exception("Kasih judul lagu / link Spotify, atau langsung judul biasa.")


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
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
            music_queues[self.guild_id] = []
            music_loop[self.guild_id] = False
            await interaction.response.send_message("⏹️ Musik dihentikan & antrean dikosongkan. (Bot tetap standby 24/7 di voice channel)", ephemeral=True)
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

# Guard agar autoplay tidak di-spawn berkali-kali (penyebab spam request -> bot di-kick Discord)
_autoplay_inflight = {}

async def _autoplay_guarded(guild_id, voice_client, channel):
    """Wrapper _autoplay_next dengan guard + backoff: satu task per guild,
    dan jeda minimal 30 detik antar percobaan supaya tidak flood gateway Discord."""
    try:
        await _autoplay_next(guild_id, voice_client, channel)
    finally:
        # Cooldown: tugas selesai, tapi tahan 30 detik sebelum watchdog boleh
        # mencoba lagi (kalau lagu tetap gagal load, tidak spam).
        async def _release():
            await asyncio.sleep(30)
            _autoplay_inflight[guild_id] = False
        asyncio.create_task(_release())

async def _autoplay_next(guild_id, voice_client, channel):
    if not voice_client or not voice_client.is_connected():
        return
    query = random.choice(AUTOPLAY_POOL)
    try:
        player = await YTDLSource.from_url(query, loop=client.loop, stream=True)
        if voice_client and voice_client.is_connected() and not voice_client.is_playing():
            voice_client.play(player, after=lambda e: play_next(guild_id, voice_client, channel))
            logger.info(f"[24/7] Autoplay memutar '{player.title}'")
            if channel:
                view = MusicControlView(guild_id)
                embed = discord.Embed(
                    title="🎵 Autoplay (Musik Rekomendasi 24/7)",
                    description=f"Antrean kosong, otomatis memutar: [{player.title}]({player.url})",
                    color=0x7289da
                )
                try:
                    await channel.send(embed=embed, view=view)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Autoplay error: {e}")

def _cleanup_old_downloads(max_age_sec=7200):
    """Hapus file audio lama di _DOWNLOAD_DIR biar disk container gak penuh.
    TTL 2 jam: lagu loop / queue panjang bisa reference file yang sama —
    TTL 15 menit dulu bikin loop replay main file yang udah dihapus."""
    try:
        now = time.time()
        for name in os.listdir(_DOWNLOAD_DIR):
            p = os.path.join(_DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(p) and (now - os.path.getmtime(p)) > max_age_sec:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def play_next(guild_id, voice_client, channel):
    _cleanup_old_downloads()
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
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
MOD_LOG_CHANNEL_NAME = os.getenv("MOD_LOG_CHANNEL", "moderator-only").strip()

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NEWSAPI_BASE_URL = "https://newsapi.org/v2"
# Model AI — bisa dioverride via env AI_MODEL di Railway tanpa push ulang.
# Fallback OpenRouter. Free pool OpenRouter itu FLAPPY: model hidup, timeout,
# 429, gantian tiap jam (tested 2026-09-15: nemotron-ultra timeout, gemma 429
# menit kemudian). Maka pakai CHAIN: coba model berurutan sampai ada yang jawab.
AI_MODEL = os.getenv("AI_MODEL", "nvidia/nemotron-3-super-120b-a12b:free").strip()
AI_FALLBACK_MODELS = [
    m.strip() for m in os.getenv(
        "AI_FALLBACK_MODELS",
        "nvidia/nemotron-3-super-120b-a12b:free,inclusionai/ling-3.0-flash-vl:free,google/gemma-4-31b-it:free,z-ai/glm-5.2:free,cohere/north-mini-code:free,dots-studio/dots-3-note-preview:free"
    ).split(",") if m.strip()
]

# Model yang baru gagal (429/timeout/kosong) di-skip sementara biar tiap pesan
# gak buang ~1 detik ngecek model yang emang lagi mati. Diukur 2026-09-17:
# gemma-4-31b & glm-5.2 konsisten 429, sedangkan nemotron-super & ling-flash jalan.
MODEL_COOLDOWN = {}
MODEL_COOLDOWN_SECONDS = 600

# Batas panjang jawaban (makin pendek = makin cepat). Bisa dioverride via env.
# CATATAN LATENSI (diukur 2026-09-17): Gemini flash free tier keluarin token
# pelan kalau di-throttle. maxOutputTokens 800 = sampai ~25 detik nunggu.
# 400 bikin jawaban 2-3 kalimat tetap muat tapi balasan balik 3x lebih cepat.
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "350"))
GEMINI_MAX_TOKENS = int(os.getenv("GEMINI_MAX_TOKENS", "400"))

# Budget waktu (detik). Total kasus terburuk = GEMINI_TIMEOUT + (OR_TIMEOUT x jumlah model).
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "15"))
OR_TIMEOUT = int(os.getenv("OR_TIMEOUT", "12"))
OR_MAX_MODELS = int(os.getenv("OR_MAX_MODELS", "3"))

# ---- Gemini API native (Google AI Studio, free tier harian) -------------
# Kalau GEMINI_API_KEY ada di env, bot pakai Gemini langsung (lebih pinter
# & stabil). Kalau tidak ada, fallback ke OpenRouter (AI_MODEL).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
# Chain model Gemini. Satu model free-tier punya quota SENDIRI, jadi kalau
# gemini-3.6-flash kena 429 (kuota harian habis), model lain masih jalan.
# Diukur 2026-09-17 (3.6-flash 429): 3.1-flash-lite 1.9s, flash-lite-latest
# 1.0s, 3.8-flash 3.8s, 3.5-flash 7.3s. Urutan = kualitas dulu, lite sebagai
# penyelamat kecepatan.
GEMINI_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-3.6-flash,gemini-flash-lite-latest,gemini-3.1-flash-lite,gemini-3.8-flash"
    ).split(",") if m.strip()
]
GEMINI_COOLDOWN = {}
GEMINI_COOLDOWN_SECONDS = int(os.getenv("GEMINI_COOLDOWN_SECONDS", "900"))
# Grounding = Gemini nge-Google jawabannya dulu sebelum jawab (data realtime).
# Makan quota lebih besar; matikan dengan GEMINI_GROUNDING=0 kalau quota boros.
GEMINI_GROUNDING = os.getenv("GEMINI_GROUNDING", "1").strip() == "1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# ---- Deteksi kebutuhan data realtime -------------------------------------
# Grounding & pencarian berita itu MAHAL (quota + latensi). Cari berita untuk
# pertanyaan kayak "apa kabar" cuma bikin prompt bengkak TANPA nambah akurasi.
# Jadi: nyalain jalur realtime HANYA kalau pertanyaannya sensitif waktu.
# Isi GEMINI_GROUNDING_MODE: "auto" (default, grounding cuma saat perlu),
# "on" (selalu), "off" (tidak pernah).
GEMINI_GROUNDING_MODE = os.getenv("GEMINI_GROUNDING_MODE", "auto").strip().lower()

_REALTIME_HINTS = (
    "hari ini", "sekarang", "terbaru", "terkini", "terupdate", "update",
    "berita", "harga", "kurs", "saham", "kripto", "cuaca",
    "skor", "hasil", "klasemen", "jadwal", "tanding", "pertandingan",
    "kapan", "siapa", "di mana", "dimana", "berapa",
    "presiden", "menteri", "gubernur", "pemilu", "politik", "pemerintah",
    "viral", "trending", "tahun ini", "bulan ini", "minggu ini",
    "kemarin", "besok", "malam ini", "tadi", "baru saja", "terakhir",
)


def butuh_realtime(teks: str) -> bool:
    """True kalau pertanyaan kemungkinan butuh data terbaru dari internet."""
    t = (teks or "").lower()
    if not t:
        return False
    return any(h in t for h in _REALTIME_HINTS)


def _grounding_aktif(teks: str) -> bool:
    """Grounding Google Search: nyala hanya saat mode auto + pertanyaan butuh realtime."""
    if GEMINI_GROUNDING_MODE == "off":
        return False
    if GEMINI_GROUNDING_MODE == "on":
        return GEMINI_GROUNDING
    return GEMINI_GROUNDING and butuh_realtime(teks)


DB_NAME = "bot_memory.db"
MAX_HISTORY_MESSAGES = 4  # Context messages (4 cukup buat nyambung, gak bikin prompt bengkak)
MAX_DB_MESSAGES = 20  # Total stored per user
PREFIX = "!"
RATE_LIMIT_SECONDS = 5  # Per user rate limit
RESPONSE_CHAR_LIMIT = 1900  # Discord message limit

# Store mod log channels per guild
mod_log_channels = {}

# Personality
# Personality — sengaja RAMPING. Model modern (Gemini 2.5 Flash) justru
# makin pinter kalau instruksinya singkat & jelas, bukan dicekik 8 aturan.
# Aturan format dihandle terpisah di build prompt (bukan di persona).
KEPRIBADIAN = (
    "Kamu Seraphine AI, asisten Discord yang asik, santai, dan ramah. "
    "Pembuatmu Notzee (sebut hanya kalau ditanya). "
    "Kamu cerdas dan berwawasan luas: jawab dengan akurat, masuk akal, dan bernas — "
    "bukan sekadar template. Bahasa Indonesia gaul tapi sopan. "
    "Jawab RINGKAS: 2-3 kalimat sebagai default. Kalau usernya minta detail/panjang/"
    "jelasin lengkap, baru keluar versi panjang. Kode tetap boleh ditulis penuh. "
    "Fakta penting: Presiden Indonesia saat ini Prabowo Subianto (sejak Oktober 2024). "
    "Kalau tidak yakin soal fakta terkini, akui — jangan mengarang."
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
#  FILTER KATA KASAR
#  Prinsip: cocok PER-KATA (bukan substring) + tahan tulisan alay.
#  Efeknya: "babi" TIDAK kena di "babin"/"babinsa", tapi "goblok",
#  "gobloknya", "anjiiing", "k0nt0l" tetap kena.
# ============================================================

# Istilah identitas (orientasi seksual / gender) ada di wordlist bawaan
# better_profanity tapi BUKAN kata kasar -> dibuang dari daftar.
EXEMPT_IDENTITY_WORDS = {
    "gay", "gays", "gayest", "homo", "homos", "homosexual", "homosexuality",
    "queer", "fag", "fags", "faggot", "faggots", "lesbian", "lesbians",
    "dyke", "dykes", "tranny", "trannies", "shemale", "shemales",
    "bisexual", "bisexuals", "trans", "transgender",
}

# Wordlist Inggris (dari better_profanity, sudah termasuk bentuk alay seperti
# "sh1t" / "he11") minus istilah identitas di atas.
try:
    _EN_WORDLIST = [
        str(w).strip().lower() for w in _bp_read_wordlist(profanity._default_wordlist_filename)
        if str(w).strip() and str(w).strip().lower() not in EXEMPT_IDENTITY_WORDS
    ]
except Exception as _e:  # kalau file wordlist hilang, filter Indonesia tetap jalan
    print(f"[WARN] Gagal baca wordlist better_profanity: {_e}")
    _EN_WORDLIST = []

# Root kata kasar Indonesia. Dicek per-kata, jadi tidak lagi salah tangkap.
CUSTOM_TOXIC_WORDS = [
    "anjing", "babi", "monyet", "setan", "bangsat", "kontol", "memek",
    "biadab", "tolol", "dungu", "goblok", "bodoh", "sampah", "hina",
    "jelek", "buruk", "sial", "sinting", "sarap",
]

# Singkatan kasar khas chat. Tambah/hapus bebas sesuai kebutuhan server.
EXTRA_TOXIC_ABBREV = [
    "anjg", "anjir", "bgst", "bngst", "gblk", "bgsd", "tll", "kntl", "kontl", "ppek",
]

# Pintu darurat: kata yang ada di wordlist Inggris tapi di bahasa Indonesia
# artinya aman. Tambah di sini kalau nanti ada salah tangkap lain.
ALLOWED_WORDS = {
    "massa",   # massa jenis / massa otot
    "dong",    # partikel ajakan: "bantuin dong"
}

# Imbuhan Indonesia yang boleh nempel di root:
# goblok -> gobloknya, menghina -> hina (root tetap ke-detect).
_ID_SUFFIXES_RAW = ("nya", "kah", "lah", "deh", "dong", "sih", "pun", "mu", "ku", "an", "kan", "in", "i")
_ID_PREFIXES_RAW = ("meng", "menge", "men", "mem", "nge", "di", "ter", "ke", "se", "ber", "peng", "pen")

# --- normalisasi -----------------------------------------------------------
# Kata Indonesia: angka/simbol alay dibuka jadi huruf (k0nt0l -> kontol).
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
    "@": "a", "$": "s", "!": "i", "|": "i", "+": "t",
})
_REPEATED = re.compile(r"(.)\1+")
_CHUNKS = re.compile(r"[a-z0-9]+(?:[._\-*][a-z0-9]+)*")
_INNER = re.compile(r"[^a-z0-9]+")
_EN_ENTRY_OK = re.compile(r"[a-z0-9\- ]+")   # entri ber-tanda baca aneh dilewati
_MASK_CHARS = "*_"
_VOWELS = "aiueo"
_MAX_MASK_VARIANTS = 32


def _squash(text: str) -> str:
    """Rapatkan huruf berulang: anjiiing -> anjing."""
    return _REPEATED.sub(r"\1", text.lower())


def _normalize_id(text: str) -> str:
    """Versi kata Indonesia: alay dibuka + huruf berulang dirapatkan."""
    return _squash(text.translate(_LEET_MAP))


def _en_forms(text: str):
    """Bentuk yang dianggap sama untuk wordlist Inggris: apa adanya + versi alay."""
    low = text.lower()
    return {_INNER.sub("", low), _INNER.sub("", low.translate(_LEET_MAP))}


_EN_SET = set()
for _w in _EN_WORDLIST:
    if _EN_ENTRY_OK.fullmatch(_w) and len(_INNER.sub("", _w)) >= 3:
        _EN_SET |= _en_forms(_w)
# istilah identitas juga dibuang dalam bentuk alay (biar "h0m0" tidak kena)
for _w in EXEMPT_IDENTITY_WORDS:
    _EN_SET -= _en_forms(_w)
_EN_SET.discard("")
_ALLOWED_EN = set()
for _w in ALLOWED_WORDS:
    _ALLOWED_EN |= _en_forms(_w)
_ALLOWED_ID = {_normalize_id(w) for w in ALLOWED_WORDS}
_TOXIC_SET = (
    {_normalize_id(w) for w in CUSTOM_TOXIC_WORDS}
    | {_normalize_id(w) for w in EXTRA_TOXIC_ABBREV}
)
_ID_SUFFIXES = tuple(_normalize_id(s) for s in _ID_SUFFIXES_RAW)
_ID_PREFIXES = tuple(_normalize_id(p) for p in _ID_PREFIXES_RAW)


def _roots_of(token: str):
    """Kandidat root dari satu kata: apa adanya, lalu tanpa imbuhan depan/belakang."""
    yield token
    for p in _ID_PREFIXES:
        if token.startswith(p) and len(token) - len(p) >= 3:
            stem = token[len(p):]
            yield stem
            for s in _ID_SUFFIXES:
                if stem.endswith(s) and len(stem) - len(s) >= 3:
                    yield stem[: -len(s)]
    for s in _ID_SUFFIXES:
        if token.endswith(s) and len(token) - len(s) >= 3:
            yield token[: -len(s)]


def _is_toxic_token(token: str) -> bool:
    """Cek satu kata: cocok mentah, atau cocok setelah imbuhan dibuang."""
    if not token or token in _ALLOWED_ID:
        return False
    if token in _TOXIC_SET:
        return True
    return any(root in _TOXIC_SET for root in _roots_of(token))


def _mask_variants(chunk: str, limit: int = _MAX_MASK_VARIANTS):
    """Kembalikan kemungkinan isi kata bertopeng: f*ck -> fack/feck/fick/fock/fuck."""
    if not any(c in chunk for c in _MASK_CHARS):
        return (chunk,)
    variants = [""]
    for ch in chunk:
        if ch in _MASK_CHARS:
            variants = [v + vowel for v in variants for vowel in _VOWELS]
        else:
            variants = [v + ch for v in variants]
        if len(variants) > limit:
            return (chunk.replace("*", "").replace("_", ""),)
    return tuple(variants)


def contains_toxic(text: str) -> bool:
    """Cek apakah teks mengandung kata kasar (wordlist Inggris + kata Indonesia)."""
    if not text:
        return False

    # Dua sumber: teks apa adanya (kata Inggris tetap utuh, mis. "sh1t") dan
    # teks yang sudah dibuka alay-nya (mis. "@njing" -> "anjing").
    for source in (text.lower(), _normalize_id(text)):
        previous = None
        for match in _CHUNKS.finditer(source):
            plain = None
            for candidate in _mask_variants(match.group(0)):
                compact = _INNER.sub("", candidate)
                if len(compact) < 3:
                    continue
                # kata Inggris (dicek per-kata, termasuk frasa 2 kata)
                if compact not in _ALLOWED_EN:
                    if compact in _EN_SET or (previous and previous + compact in _EN_SET):
                        return True
                # kata Indonesia (imbuhan dibuang)
                if _is_toxic_token(_normalize_id(candidate)):
                    return True
                if plain is None:
                    plain = compact
            previous = plain

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
        name="🧠 Cortex Tools (langsung ketik, tanpa command)",
        value="`25 x 4`, `17% dari 200`, `akar 144` - Kalkulator\n"
              "`https://...` di pesan - Baca & rangkum link\n"
              "`gambar kucing lucu` - Bikin gambar",
        inline=False
    )
    embed.add_field(
        name="🎵 Musik (Slash)",
        value="`/splay` - Putar dari YouTube / Spotify\n`/squeue` - Lihat antrean\n`/sskip` - Lewati lagu\n"
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
        value=f"Dibuat oleh: **Notzee**\nNama: **Seraphine AI**\nModel: **{(GEMINI_MODELS[0] if GEMINI_API_KEY else AI_MODEL)}**",
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

@tree.command(name="splay", description="Putar musik dari YouTube / Spotify (Seraphine)")
@app_commands.describe(query="Judul lagu, URL YouTube, atau link Spotify")
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
            voice_client = await member.voice.channel.connect(self_deaf=True, reconnect=True)

        # Aktifkan persistensi 24/7 di channel ini agar bot tidak pernah keluar
        try:
            set_voice_247(interaction.guild.id, member.voice.channel.id, interaction.channel.id)
        except Exception:
            pass

        # --- Spotify detect: link open.spotify.com / awalan 'spotify:' ---
        spotify_label = None
        spotify_queries: list[str] = [query]
        if _is_spotify_url(query) or re.match(r"(?i)^\s*spotify\s*:\s*", query or ""):
            # Coba Web API dulu (kalau kredensial ada). Kalau gagal / gak ada
            # kredensial (atau owner app belum Premium -> 403), fallback ke
            # scrape halaman publik Spotify. YouTube tetap jadi sumber audio.
            async def _try_api():
                return await client.loop.run_in_executor(
                    None, resolve_spotify_query, query
                )

            async def _try_scrape():
                return await client.loop.run_in_executor(
                    None, resolve_spotify_scrape, query
                )

            resolved = False
            if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
                try:
                    spotify_queries, spotify_label = await _try_api()
                    resolved = True
                except Exception as e:
                    ytdl_log.warning(f"Spotify API gagal, fallback scrape: {e}")
            if not resolved:
                try:
                    spotify_queries, spotify_label = await _try_scrape()
                    resolved = True
                except Exception as e:
                    await interaction.followup.send(
                        f"❌ Spotify gagal: {e}\n"
                        "💡 Tips: kirim judul lagu biasa, atau link YouTube.",
                        ephemeral=True,
                    )
                    return

        first_spotify_note = spotify_label  # ditempel di embed lagu pertama
        successful_tracks = 0
        queued_titles: list[str] = []  # buat ringkasan lagu yang masuk antrean

        for idx, yt_query in enumerate(spotify_queries):
            try:
                player = await YTDLSource.from_url(yt_query, loop=client.loop, stream=True)
            except Exception as e:
                ytdl_log.warning(f"Gagal extract lagu ({yt_query}): {e}")
                continue

            successful_tracks += 1

            if successful_tracks == 1 and not voice_client.is_playing():
                voice_client._last_source = player
                voice_client.play(player, after=lambda e: play_next(interaction.guild.id, voice_client, interaction.channel))
                embed = discord.Embed(
                    title="🎵 Sekarang Diputar",
                    description=f"[{player.title}]({player.url})",
                    color=0x7289da
                )
                if first_spotify_note:
                    embed.set_footer(text=f"🔗 via {first_spotify_note}")
                    first_spotify_note = None
                view = MusicControlView(interaction.guild.id)
                try:
                    await interaction.followup.send(embed=embed, view=view)
                except:
                    await interaction.channel.send(embed=embed, view=view)
            else:
                music_queues[interaction.guild.id].append(player)
                queued_titles.append(player.title or yt_query)
                # Notif per-lagu HANYA untuk single song (bukan playlist),
                # playlist dirangkum sekali di akhir biar gak spam.
                if len(spotify_queries) == 1 and successful_tracks == 1:
                    msg = f"✅ Menambahkan ke antrean: **{player.title}** (Urutan ke-{len(music_queues[interaction.guild.id])})"
                    try:
                        await interaction.followup.send(msg)
                    except:
                        await interaction.channel.send(msg)

        # Ringkasan sekali untuk playlist/album yang masuk antrean
        if queued_titles and len(spotify_queries) > 1:
            preview = "\n".join(f"`{i+1}.` {t}" for i, t in enumerate(queued_titles[:10]))
            extra = f"\n…dan {len(queued_titles) - 10} lagu lagi" if len(queued_titles) > 10 else ""
            try:
                await interaction.channel.send(
                    f"📥 **{len(queued_titles)} lagu masuk antrean:**\n{preview}{extra}"
                )
            except:
                pass

        if successful_tracks == 0:
            await interaction.followup.send("❌ Gagal memutar lagu dari Spotify/YouTube (semua lagu tidak ditemukan).", ephemeral=True)
            return

        # Kabari kalau playlist/album Spotify masuk antrean banyak
        if spotify_label and len(spotify_queries) > 1:
            try:
                await interaction.channel.send(
                    f"✅ **{spotify_label}** masuk antrean bro! {successful_tracks} lagu berhasil dimuat. "
                    f"Cek `/squeue` 😉"
                )
            except:
                pass

    except Exception as e:
        logger.error(f"Music Error: {e}\n{traceback.format_exc()}")
        # Mode 24/7: Bot tetap berada di voice channel meski terjadi error putar lagu

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

@tree.command(name="sstop", description="Stop musik & bersihkan antrean (Bot tetap standby 24/7)")
async def slash_stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        music_queues[interaction.guild.id] = []
        music_loop[interaction.guild.id] = False
        await interaction.response.send_message("⏹️ Musik dihentikan & antrean dikosongkan. Bot tetap standby di voice channel 24/7 bro.")
    else:
        await interaction.response.send_message("❌ Bot lagi gak ada di voice channel.", ephemeral=True)

@tree.command(name="sleave", description="Keluarkan bot dari voice channel & matikan mode 24/7")
async def slash_leave(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Hanya bisa digunakan di server bro!", ephemeral=True)
        return
    remove_voice_247(interaction.guild.id)
    voice_client = interaction.guild.voice_client
    if voice_client:
        music_queues[interaction.guild.id] = []
        music_loop[interaction.guild.id] = False
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await voice_client.disconnect(force=True)
        await interaction.response.send_message("👋 Bot telah keluar dari voice channel dan mode 24/7 dimatikan.")
    else:
        await interaction.response.send_message("❌ Bot lagi gak ada di voice channel.", ephemeral=True)

@tree.command(name="s247", description="Kunci mode 24/7 agar Seraphine tidak pernah keluar voice channel")
@app_commands.describe(status="Status 24/7: on (kunci 24/7) atau off (matikan)")
@app_commands.choices(status=[
    app_commands.Choice(name="🟢 ON - Standby 24/7 di channel ini", value="on"),
    app_commands.Choice(name="🔴 OFF - Matikan mode 24/7", value="off")
])
async def slash_247(interaction: discord.Interaction, status: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Hanya bisa digunakan di server bro!", ephemeral=True)
        return
    
    if status == "on":
        member = interaction.guild.get_member(interaction.user.id)
        target_vc = member.voice.channel if (member and member.voice) else None
        if not target_vc and interaction.guild.voice_client:
            target_vc = interaction.guild.voice_client.channel
        
        if not target_vc:
            await interaction.response.send_message("❌ Kamu harus masuk voice channel dulu untuk mengunci bot 24/7 di sana!", ephemeral=True)
            return

        set_voice_247(interaction.guild.id, target_vc.id, interaction.channel.id)
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_connected():
            await target_vc.connect(self_deaf=True, reconnect=True)
        elif interaction.guild.voice_client.channel != target_vc:
            await interaction.guild.voice_client.move_to(target_vc)
            
        await interaction.response.send_message(f"🔒 **Mode 24/7 AKTIF!** Seraphine terkunci di voice channel **{target_vc.name}** dan otomatis reconnect jika terputus.")
    else:
        remove_voice_247(interaction.guild.id)
        await interaction.response.send_message("🔓 **Mode 24/7 DIMATIKAN.** (Bot tidak akan auto-reconnect lagi jika disconnect).")

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
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            channel_id INTEGER DEFAULT 0
        )''')
        # Migration: tambah channel_id utk DB lama
        try:
            c.execute("SELECT channel_id FROM conversation LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE conversation ADD COLUMN channel_id INTEGER DEFAULT 0")
            logger.info("Migrated conversation table: added channel_id column")
        
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

        # Voice 24/7 persistence table
        c.execute('''CREATE TABLE IF NOT EXISTS voice_247 (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            text_channel_id INTEGER DEFAULT 0
        )''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

# Initialize DB on module load so dashboard/Railway imports create tables automatically
init_db()

def set_voice_247(guild_id: int, channel_id: int, text_channel_id: int = 0):
    """Simpan channel voice 24/7 ke SQLite agar bot auto-reconnect saat startup / terputus."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO voice_247 (guild_id, channel_id, text_channel_id) VALUES (?, ?, ?)',
                  (guild_id, channel_id, text_channel_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error set_voice_247: {e}")

def get_all_voice_247():
    """Ambil semua konfigurasi voice 24/7 yang aktif."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('SELECT guild_id, channel_id, text_channel_id FROM voice_247')
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception:
        return []

def remove_voice_247(guild_id: int):
    """Hapus setting voice 24/7 untuk guild."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('DELETE FROM voice_247 WHERE guild_id = ?', (guild_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error remove_voice_247: {e}")

def save_conversation(user_id: int, user_msg: str, bot_response: str, channel_id: int = 0):
    """Save conversation to database."""
    try:
        # init_db() TIDAK dipanggil di sini lagi: itu bikin CREATE TABLE +
        # commit + log di SETIAP balasan (di jalur panas). Tabel sudah dibuat
        # sekali saat module load (lihat init_db() tepat di atas).
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('INSERT INTO conversation (user_id, user_message, bot_response, channel_id) VALUES (?, ?, ?, ?)',
                  (user_id, user_msg, bot_response, channel_id))
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

def get_user_history(user_id: int, limit: int = MAX_HISTORY_MESSAGES, channel_id: int = 0) -> str:
    """Get user conversation history. Prefers context from the same channel."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute('''SELECT user_message, bot_response, channel_id FROM conversation 
                     WHERE user_id = ? ORDER BY id DESC LIMIT ?''',
                  (user_id, limit * 4))
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        # Prioritas: pesan di channel yang sama (terbaru dulu), fallback campuran
        if channel_id:
            same_channel = [(m, r) for (m, r, ch) in rows if ch == channel_id][:limit]
        else:
            same_channel = []
        picked = same_channel if len(same_channel) >= 1 else [(m, r) for (m, r, ch) in rows[:limit]]
        picked = picked[::-1]  # rows DESC -> balik ke urutan kronologis (lama -> baru)
        
        history = []
        for user_msg, bot_resp in picked:
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

NEWS_CACHE = {"time": 0, "text": ""}
NEWS_CACHE_TTL = 900  # 15 menit (dulu 30 — keburu basi buat topik cepat)

_WEB_SEARCH_CACHE = {}  # query -> (timestamp, text)
# Berita cepat basi. 15 menit bikin jawaban "hari ini" masih nunjuk berita lama.
WEB_SEARCH_TTL = int(os.getenv("WEB_SEARCH_TTL", "240"))


def _parse_pubdate(teks: str):
    """Parse pubDate RSS (RFC 822) -> datetime aware, atau None kalau gagal."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(teks)
    except Exception:
        return None


def _gnews_items(query: str, extra: str = "") -> list:
    """Ambil item Google News RSS buat query + extra operator. Return list dict."""
    import urllib.parse
    import xml.etree.ElementTree as _ET
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query + extra) + "&hl=id&gl=ID&ceid=ID:id")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
    if r.status_code != 200:
        return []
    root = _ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        judul = (it.findtext("title") or "").strip()
        if not judul:
            continue
        out.append({
            "judul": judul,
            "tgl": (it.findtext("pubDate") or "").strip(),
            "sumber": (it.findtext("source") or "").strip(),
            "dt": _parse_pubdate((it.findtext("pubDate") or "").strip()),
        })
    return out


def fetch_web_context(query: str, max_items: int = 4, realtime: bool = True) -> str:
    """Cari info TERKINI via Google News RSS (gratis, tanpa API key, cloud-safe).

    Kalau realtime=True, pakai operator `when:1d` supaya Google cuma balikin
    berita <=24 jam dan urutannya yang paling baru dulu. Tanpa filter ini Google
    balikin artikel lama (kejadian 1-3 hari sebelumnya) — itu sebabnya jawaban
    bot kelihatan basi. Kalau kosong, fallback: ambil tanpa filter tapi buang
    item lebih tua dari 5 hari dan urutkan dari yang terbaru.
    Selalu return string (gak pernah throw)."""
    import time as _time
    q = (query or "").strip()[:120]
    if not q:
        return ""
    cache_key = f"{q}|1d" if realtime else q
    now = _time.time()
    hit = _WEB_SEARCH_CACHE.get(cache_key)
    if hit and (now - hit[0]) < WEB_SEARCH_TTL:
        return hit[1]

    try:
        items = _gnews_items(q, " when:1d") if realtime else []
        if not items:
            items = _gnews_items(q, "")
        if realtime and items:
            # Buang item basi (>5 hari). Kalau semua basi, buang semuanya:
            # lebih baik AI jawab dari pengetahuan sendiri daripada disuapin
            # berita berbulan-bulan lalu dan dikira "terbaru".
            from datetime import timezone as _tz
            cutoff = datetime.now(_tz.utc) - timedelta(days=5)
            items = [i for i in items if i["dt"] and i["dt"] > cutoff]

        # Urutkan paling baru dulu (item tanpa tanggal ditaruh paling belakang).
        items.sort(key=lambda i: i["dt"].timestamp() if i["dt"] else 0, reverse=True)

        out = []
        for it in items[:max_items]:
            baris = f"- {it['judul']}"
            meta = ", ".join(x for x in (it["sumber"], it["tgl"]) if x)
            if meta:
                baris += f" ({meta})"
            out.append(baris)
        teks = "\n".join(out)
        if teks:
            _WEB_SEARCH_CACHE[cache_key] = (now, teks)
        return teks
    except Exception as e:
        logger.warning(f"Web search RSS gagal: {str(e)[:60]}")
        return ""


def format_tanggal_indo(dt=None) -> str:
    """Tanggal + jam lengkap format Indonesia, dipakai buat anchor 'hari ini'."""
    dt = dt or datetime.now()
    hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][dt.weekday()]
    bulan = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember"][dt.month - 1]
    return f"{hari}, {dt.day} {bulan} {dt.year} pukul {dt.strftime('%H:%M')} WIB"


# ---- Pembersih query pencarian -------------------------------------------
# Google News itu mesin pencari ARTIKEL, bukan penjawab pertanyaan. Query
# "siapa menkeu indonesia sekarang" cuma balikin artikel OPINI soal pergantian
# pejabat, bukan fakta "Suahasil Nazara dilantik jadi Menkeu". Query "menteri
# keuangan indonesia" balikin faktanya. Jadi buang kata tanya + singkatan.
_QUERY_STOPWORDS = {
    "siapa", "apa", "apakah", "kapan", "dimana", "mana", "berapa", "bagaimana",
    "kenapa", "mengapa", "yang", "dan", "atau", "itu", "ini", "sekarang",
    "hari", "terbaru", "terkini", "terupdate", "dong", "ya", "nih", "sih",
    "kah", "deh", "gitu", "aja", "saja", "tolong", "coba", "bro", "bang",
    "min", "kak", "gua", "gue", "aku", "saya", "kamu", "lu", "lo", "bot",
    "seraphine", "adalah", "sebutkan", "jelaskan", "info", "kabar", "tentang",
    "soal", "mengenai", "pada", "untuk", "dari", "ke", "di", "adakah",
    "benar", "bener", "emang", "memang", "sih", "kalian", "kita", "sini",
    "tau", "tahu", "gak", "ga", "tidak", "nggak", "engga", "tadi", "malam",
    "pagi", "siang", "sore", "kemarin", "besok", "dulu", "lagi", "udah",
    "sudah", "belum", "punya", "ada", "gimana", "kok", "kan", "lah",
}
# Singkatan berita -> bentuk panjang (kunci multi-kata duluan biar gak setengah ganti).
_QUERY_ALIASES = {
    "presiden ri": "presiden indonesia",
    "menteri keuangan": "menteri keuangan",
    "menkeu": "menteri keuangan",
    "menhan": "menteri pertahanan",
    "menlu": "menteri luar negeri",
    "mendag": "menteri perdagangan",
    "menteri": "menteri",
    "kapolri": "kapolri",
    "prabowo": "prabowo subianto",
    "jokowi": "joko widodo",
}


def build_search_query(teks: str, maks_kata: int = 6) -> str:
    """Ubah pertanyaan user jadi query berita yang efektif.

    'siapa menkeu indonesia sekarang' -> 'menteri keuangan indonesia'
    Kosong/gagal -> balikin teks aslinya (biar tetap ada yang dicari).
    """
    asli = (teks or "").strip()
    if not asli:
        return ""
    bersih = re.sub(r"[^\w\s]", " ", asli.lower())
    kata = [w for w in bersih.split() if w and w not in _QUERY_STOPWORDS]
    if not kata:
        return asli[:120]
    q = " ".join(kata[:maks_kata])
    # Alias: frasa panjang dulu supaya "presiden ri" gak jadi "presiden indonesia"
    # setelah "menteri keuangan" keburu ke-substitusi.
    for kunci in sorted(_QUERY_ALIASES, key=len, reverse=True):
        q = re.sub(rf"\b{re.escape(kunci)}\b", _QUERY_ALIASES[kunci], q)
    return q.strip()[:120]


def fetch_trending_news() -> str:
    """Ambil berita terkini dari RSS feed Indonesia (gratis, tanpa API key, cloud-safe).
    SELALU mengembalikan string (gak pernah throw), cache 30 menit."""
    import time as _time
    now = _time.time()
    if NEWS_CACHE["text"] and (now - NEWS_CACHE["time"]) < NEWS_CACHE_TTL:
        return NEWS_CACHE["text"]
    try:
        feeds = [
            "https://www.cnnindonesia.com/nasional/rss",
            "https://rss.tempo.co/nasional",
        ]
        import re as _re
        import xml.etree.ElementTree as _ET
        judul_list = []
        for url in feeds:
            if len(judul_list) >= 6:
                break
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                root = _ET.fromstring(r.content)
                for item in root.iter("item"):
                    t = item.find("title")
                    if t is not None and t.text:
                        judul = _re.sub(r"\s+", " ", t.text).strip()
                        if judul and judul not in judul_list:
                            judul_list.append(judul)
                    if len(judul_list) >= 6:
                        break
            except Exception as e:
                logger.warning(f"RSS gagal {url}: {str(e)[:50]}")
                continue
        if not judul_list:
            return ""
        teks = "Berita Terbaru:\n"
        for i, j in enumerate(judul_list[:6], 1):
            teks += f"{i}. {j}\n"
        logger.info(f"News RSS fetched: {len(judul_list)} judul")
        NEWS_CACHE["text"] = teks
        NEWS_CACHE["time"] = _time.time()
        return teks
    except Exception as e:
        logger.error(f"Error fetch RSS: {e}")
        return ""


# ============================================================
#  AI FUNCTIONS (MERGED & OPTIMIZED)
# ============================================================

async def tanya_ai(pertanyaan: str, user_id: int, user_name: str, include_trending: bool = False, channel_id: int = 0) -> str:
    """
    Send question to OpenRouter with optional trending news context.
    Merged function replacing both tanya_ai and tanya_ai_dengan_trending.
    """
    try:
        # Build context
        context_parts = [KEPRIBADIAN]

        # Apakah pertanyaan ini butuh data terbaru dari internet?
        perlu = butuh_realtime(pertanyaan)

        # SELALU inject tanggal real-time biar model tau tahun berapa.
        # Ditaruh di ATAS (paling awal) supaya model gak "lupa" anchor waktunya.
        context_parts.insert(0, "KONTEKS WAKTU (paling penting): sekarang "
                                + format_tanggal_indo()
                                + ". Semua kata 'hari ini', 'sekarang', 'terbaru' "
                                  "merujuk ke waktu ini, bukan ke masa trainingmu.")

        if include_trending and perlu:
            # Jalankan di thread terpisah supaya event loop tetap responsif.
            # Cuma dipanggil kalau pertanyaannya memang butuh info terkini —
            # nempelin 6 headline ke SEMUA chat bikin prompt bengkak & lambat.
            berita = await asyncio.to_thread(fetch_trending_news)
            if berita:
                context_parts.append(f"Berita Trending Saat Ini:\n{berita}")

        # Web search realtime: cari info terkini soal PERTANYAAN user di
        # Google News, inject hasilnya ke prompt. Ini bikin jawaban gak
        # basi walau modelnya punya knowledge cutoff lama.
        # Dijalankan HANYA kalau pertanyaannya sensitif waktu — biar chat
        # biasa (yang gak butuh berita) balas secepat mungkin.
        if perlu:
            q_cari = build_search_query(pertanyaan)
            web_ctx = await asyncio.to_thread(fetch_web_context, q_cari, 5, True)
            if web_ctx:
                context_parts.append(
                    "Hasil pencarian BERITA TERKINI (Google News, kueri: "
                    + q_cari + ", cuma 24 jam terakhir, urut dari yang paling "
                    "baru). Pakai ini sebagai fakta terbaru; kalau bentrok dengan "
                    "ingatanmu, PRIORITASKAN ini dan sebut tanggalnya:\n" + web_ctx
                )
        
        history = get_user_history(user_id, channel_id=channel_id)
        if history:
            context_parts.append(f"Recent context:\n{history}")
        
        context_parts.append(f"User {user_name} bertanya: {pertanyaan}")
        full_prompt = "\n\n".join(context_parts)

        # ---------- Jalur 1: Gemini API native (kalau GEMINI_API_KEY ada) ----------
        if GEMINI_API_KEY:
            pakai_grounding = _grounding_aktif(pertanyaan)
            _gnm = time.time()
            _gem_chain = [GEMINI_MODEL] + [m for m in GEMINI_MODELS if m != GEMINI_MODEL]
            _gem_live = [m for m in _gem_chain if GEMINI_COOLDOWN.get(m, 0) <= _gnm]
            if not _gem_live:
                GEMINI_COOLDOWN.clear()
                _gem_live = _gem_chain
            for gmodel in _gem_live:
                gdata = {
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": {"temperature": 0.7, "maxOutputTokens": GEMINI_MAX_TOKENS},
                }
                # Google Search Grounding: jawaban berbasis hasil pencarian
                # realtime, bukan cuma ingatan training model. Dinyalakan
                # selektif (lihat _grounding_aktif) biar quota gak jebol.
                if pakai_grounding:
                    gdata["tools"] = [{"google_search": {}}]
                logger.info(f"Requesting Gemini ({gmodel}) for user {user_id}"
                            f" (grounding={'on' if pakai_grounding else 'off'}, max_tok={GEMINI_MAX_TOKENS})")
                _t0 = time.time()
                try:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(
                            requests.post,
                            f"{GEMINI_BASE_URL}/models/{gmodel}:generateContent",
                            params={"key": GEMINI_API_KEY},
                            json=gdata,
                            headers={"Content-Type": "application/json"},
                            timeout=GEMINI_TIMEOUT,
                        ),
                        timeout=GEMINI_TIMEOUT + 5,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Gemini timeout: {gmodel} (>{GEMINI_TIMEOUT}s)")
                    GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS
                    continue
                except requests.exceptions.ConnectionError:
                    logger.error("Connection error ke Gemini — fallback OpenRouter")
                    break
                except Exception as e:
                    logger.error(f"Gemini error {gmodel}: {str(e)[:80]}")
                    GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS
                    continue
                try:
                    ghasil = res.json()
                except Exception:
                    GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS
                    continue
                if res.status_code == 429:
                    logger.warning(f"Gemini quota habis 429: {gmodel} — cooldown, coba model Gemini lain")
                    GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS
                    continue
                if "error" in ghasil:
                    logger.error(f"Gemini error ({gmodel}): {str(ghasil['error'].get('message', '?'))[:120]}")
                    GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS
                    continue
                candidates = ghasil.get("candidates") or []
                parts = ((candidates[0] if candidates else {}).get("content") or {}).get("parts") or []
                balasan = "".join(p.get("text", "") for p in parts).strip()
                if balasan:
                    GEMINI_COOLDOWN.pop(gmodel, None)
                    save_conversation(user_id, pertanyaan, balasan, channel_id=channel_id)
                    logger.info(f"Gemini response saved for user {user_id} "
                                f"in {round(time.time() - _t0, 2)}s via {gmodel} "
                                f"(out_tok={ghasil.get('usageMetadata', {}).get('candidatesTokenCount', '?')})")
                    return balasan
                logger.warning(f"Gemini balasan kosong ({gmodel}) — coba model Gemini lain")
                GEMINI_COOLDOWN[gmodel] = time.time() + GEMINI_COOLDOWN_SECONDS

        # ---------- Jalur 2: OpenRouter (fallback / default tanpa key) ----------
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://discord.com",
            "X-Title": "Seraphine AI Bot"
        }

        # Chain: free pool OpenRouter flappy (429/timeout gantian), jadi coba
        # beberapa model berurutan sampai ada yang jawab.
        models_to_try = [AI_MODEL] + [m for m in AI_FALLBACK_MODELS if m != AI_MODEL]
        # Skip model yang lagi cooldown (baru 429/timeout) biar gak buang waktu tiap pesan.
        _now = time.time()
        _fresh = [m for m in models_to_try if MODEL_COOLDOWN.get(m, 0) <= _now]
        if _fresh:
            _skipped = [m for m in models_to_try if m not in _fresh]
            if _skipped:
                logger.info("Skip model cooldown: " + ", ".join(_skipped))
            models_to_try = _fresh
        else:
            MODEL_COOLDOWN.clear()  # semua kena cooldown -> reset, coba dari awal
        # Cap jumlah model: tiap model yang mati makan OR_TIMEOUT detik. Tanpa cap,
        # 6 model mati = user nunggu >1 menit buat dapet pesan error.
        models_to_try = models_to_try[:OR_MAX_MODELS]
        last_error = "no model tried"

        for model in models_to_try:
            data = {
                "model": model,
                "messages": [{"role": "user", "content": full_prompt}],
                "temperature": 0.7,
                "max_tokens": AI_MAX_TOKENS,  # dibatasi biar jawaban gak kepanjangan (lambat)
            }
            logger.info(f"Requesting AI response for user {user_id} (model: {model})")
            _t0 = time.time()
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(
                        requests.post,
                        f"{OPENROUTER_BASE_URL}/chat/completions",
                        json=data,
                        headers=headers,
                        timeout=OR_TIMEOUT
                    ),
                    timeout=OR_TIMEOUT + 4
                )
            except (asyncio.TimeoutError, requests.exceptions.Timeout):
                logger.warning(f"OpenRouter timeout: {model} ({round(time.time()-_t0,2)}s)")
                MODEL_COOLDOWN[model] = time.time() + MODEL_COOLDOWN_SECONDS
                last_error = f"{model}: timeout"
                continue
            except requests.exceptions.ConnectionError:
                logger.warning(f"Connection error: {model}")
                MODEL_COOLDOWN[model] = time.time() + MODEL_COOLDOWN_SECONDS
                last_error = f"{model}: connection"
                continue

            hasil = res.json()
            if "error" in hasil:
                error_msg = hasil["error"].get("message", "Unknown error")
                logger.warning(f"OpenRouter error ({model}): {error_msg[:80]}")
                MODEL_COOLDOWN[model] = time.time() + MODEL_COOLDOWN_SECONDS
                last_error = f"{model}: {error_msg[:60]}"
                continue  # model mati/limit — coba model berikutnya, skip sementara

            balasan = hasil.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if balasan:
                MODEL_COOLDOWN.pop(model, None)
                save_conversation(user_id, pertanyaan, balasan, channel_id=channel_id)
                logger.info(f"Response saved for user {user_id} (via {model}, "
                            f"{round(time.time()-_t0,2)}s)")
                return balasan
            # jawaban kosong = model aneh, lanjut model berikutnya
            MODEL_COOLDOWN[model] = time.time() + MODEL_COOLDOWN_SECONDS
            last_error = f"{model}: empty response"

        logger.error(f"Semua model gagal: {last_error}")
        return "⏱️ AI sedang load, coba lagi dalam beberapa detik"

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

_WATCHDOG_TASK = None

async def voice_247_watchdog():
    """Background watchdog: menjaga bot tetap di voice channel 24/7 dan auto-reconnect."""
    await client.wait_until_ready()
    logger.info("[24/7] Voice 24/7 watchdog loop aktif.")
    while not client.is_closed():
        try:
            records = get_all_voice_247()
            env_gid = os.environ.get("VOICE_247_GUILD_ID")
            env_cid = os.environ.get("VOICE_247_CHANNEL_ID")
            if env_gid and env_cid:
                try:
                    eg = int(env_gid)
                    ec = int(env_cid)
                    if not any(r[0] == eg for r in records):
                        records.append((eg, ec, 0))
                except Exception:
                    pass

            for guild_id, channel_id, text_channel_id in records:
                guild = client.get_guild(guild_id)
                if not guild:
                    continue
                vchannel = guild.get_channel(channel_id)
                if not vchannel or not isinstance(vchannel, (discord.VoiceChannel, discord.StageChannel)):
                    continue

                vc = guild.voice_client
                # 1. Jika belum connect atau terputus, reconnect otomatis
                if not vc or not vc.is_connected():
                    # Cek permission bot dulu: kalau tidak punya Connect/Speak,
                    # skip tanpa spam log (mis. channel stage yang butuh request-to-speak).
                    try:
                        me = guild.me
                        perms = vchannel.permissions_for(me) if me else None
                        if perms and not perms.connect:
                            logger.warning(f"[24/7] Tidak punya izin Connect ke '{vchannel.name}', skip.")
                            continue
                        if perms and not perms.speak:
                            logger.warning(f"[24/7] Tidak punya izin Speak di '{vchannel.name}', skip.")
                            continue
                    except Exception:
                        pass
                    try:
                        logger.info(f"[24/7] Auto-reconnecting ke voice channel '{vchannel.name}' di {guild.name}")
                        if vc:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                        vc = await vchannel.connect(self_deaf=True, reconnect=True, timeout=20.0)
                        logger.info(f"[24/7] ✅ Berhasil connect ke '{vchannel.name}'")
                    except Exception as e:
                        logger.warning(f"[24/7] Gagal auto-reconnect ke {vchannel.name}: {e}")
                        continue

                # 2. Jika connected tapi di channel yang salah, pindahkan
                if vc and vc.channel != vchannel:
                    try:
                        await vc.move_to(vchannel)
                    except Exception:
                        pass

                # 3. Jika connected tapi lagu sedang kosong / berhenti, picu autoplay agar tidak hening
                #    Guard: hanya picu kalau tidak ada task autoplay yang sedang jalan,
                #    supaya tidak spam request (penyebab bot ke-kick Discord).
                if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
                    if not music_queues[guild_id] and not _autoplay_inflight.get(guild_id):
                        _autoplay_inflight[guild_id] = True
                        t_channel = guild.get_channel(text_channel_id) if text_channel_id else None
                        asyncio.create_task(_autoplay_guarded(guild_id, vc, t_channel))
        except Exception as e:
            logger.error(f"[24/7] Watchdog error: {e}")
        await asyncio.sleep(20)

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

    # Start 24/7 Voice Watchdog loop jika belum jalan
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is None or _WATCHDOG_TASK.done():
        _WATCHDOG_TASK = asyncio.create_task(voice_247_watchdog())
        logger.info("✅ [24/7] Voice Watchdog background task aktif!")

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

# ============================================================
#  AUTO-ROLE: kasih role ke member baru
#  Config key: autorole_enabled, autorole_role_id
# ============================================================
DEFAULT_AUTOROLE_ROLE_ID = 1533458688667549706  # friends Seraa

@client.event
async def on_member_join(member):
    cfg = load_bot_config()
    if not cfg.get("autorole_enabled", True):
        return
    if member.bot:
        return

    role_id = cfg.get("autorole_role_id", DEFAULT_AUTOROLE_ROLE_ID)
    try:
        role_id = int(role_id)
    except (TypeError, ValueError):
        logger.error(f"[AUTOROLE] role_id tidak valid: {role_id!r}")
        return

    role = member.guild.get_role(role_id)
    if role is None:
        logger.error(f"[AUTOROLE] role {role_id} tidak ada di guild {member.guild.id}")
        return

    try:
        await member.add_roles(role, reason="Auto-role: member baru join")
        logger.info(f"[AUTOROLE] {member.name} ({member.id}) dapat role {role.name}")
    except discord.Forbidden:
        logger.error(
            f"[AUTOROLE] Gagal kasih role {role.name} ke {member.name} - "
            f"cek posisi role bot vs role target + izin Manage Roles"
        )
    except Exception as e:
        logger.error(f"[AUTOROLE] Error: {e}")

@client.event
async def on_message(pesan):
    if pesan.author.bot:
        return
    
    cfg = load_bot_config()
    exempt_mods = cfg.get("automod_exempt_mods", True)

    # ============================================================
    #  TOXIC MESSAGE AUTO-DELETE
    #  - Hanya di server (DM tidak bisa dihapus & tidak ada mod log)
    #  - Mod/admin dikecualikan: kalau bot tidak punya izin hapus pesannya,
    #    bot jangan malah reply publik di channel
    # ============================================================
    if (
        pesan.guild
        and cfg.get("automod_enabled", True)
        and not (exempt_mods and is_moderator(pesan.author))
        and contains_toxic(pesan.content)
    ):
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
    
    # Kalau bukan command / bukan mention bot, abaikan (jangan rate limit chat biasa member!)
    if not pertanyaan:
        return
    
    # ============================================================
    #  RATE LIMIT CHECK (Hanya untuk pesan yang manggil bot/AI)
    # ============================================================
    if not check_rate_limit(pesan.author.id):
        logger.warning(f"Rate limit hit for user {pesan.author.id} ({pesan.author.name})")
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
                include_trending=True,
                channel_id=pesan.channel.id
            )
        
        jawaban = truncate_response(jawaban)
        embed = create_response_embed("🔥 Trending Hari Ini", jawaban)
        await pesan.reply(embed=embed)
        return
    
    # ============================================================
    #  SERVER INFO COMMAND
    # ============================================================
    if command == "server-info":
        if not pesan.guild:
            await pesan.reply("❌ Command ini cuma bisa dipakai di server bro!")
            return
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
        if not pesan.guild:
            await pesan.reply("❌ Command ini cuma bisa dipakai di server bro!")
            return
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
        if not pesan.guild:
            await pesan.reply("❌ Command ini cuma bisa dipakai di server bro!")
            return
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
        if not pesan.guild:
            await pesan.reply("❌ Command ini cuma bisa dipakai di server bro!")
            return
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

    # ---- CORTEX TOOLS: kalkulator / baca link / bikin gambar ----
    # Dicek SEBELUM jalur AI biasa. Kalau bukan tugas tools -> None ->
    # lanjut ke tanya_ai seperti biasa. Kalau Cortex gagal internal,
    # dia balikin None juga (fail-open), jadi chat gak pernah macet.
    if cfg.get("cortex_enabled", True):
        try:
            hasil_cortex = await cortex_handle(pertanyaan)
        except Exception as _cx:
            logger.error(f"Cortex handle error: {_cx}")
            hasil_cortex = None
        if hasil_cortex:
            if hasil_cortex.get("image_url"):
                embed = discord.Embed(description=hasil_cortex["text"],
                                      color=0x7289da, timestamp=datetime.now())
                embed.set_image(url=hasil_cortex["image_url"])
                await pesan.reply(embed=embed)
            else:
                jawaban = truncate_response(hasil_cortex["text"])
                await pesan.reply(embed=create_response_embed("💬 Jawaban", jawaban))
            return

    async with pesan.channel.typing():
        jawaban = await tanya_ai(pertanyaan, pesan.author.id, pesan.author.name, include_trending=True, channel_id=pesan.channel.id)
    
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
    if not DISCORD_TOKEN or not OPENROUTER_API_KEY:
        logger.error("❌ Missing environment variables! Check .env file")
        exit(1)
    
    logger.info("Starting bot...")
    init_db()
    
    try:
        client.run(DISCORD_TOKEN)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        exit(1)
