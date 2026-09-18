"""
CORTEX v2 — modul tools Seraphine AI.
Deteksi & eksekusi: kalkulator, baca link (rangkum), bikin gambar.
Dipanggil dari on_message SEBELUM jalur AI biasa; balikin str jawaban,
atau None kalau pesan bukan tugas tools (lanjut ke tanya_ai biasa).

Desain:
- Zero dependensi baru (requests sudah ada di requirements).
- Rangkuman link pakai Gemini chain yang sama kayak bot (key dari env).
- Gambar via Pollinations.ai (gratis, tanpa key) — Discord render URL langsung.
- Kalkulator: AST whitelist, bukan eval mentah.
"""

import ast
import logging
import math
import os
import re
import time
import urllib.parse

import requests

logger = logging.getLogger(__name__)

# ---- Konfigurasi (bisa dioverride env di Railway) -------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
CORTEX_GEMINI_MODEL = os.getenv("CORTEX_GEMINI_MODEL", "gemini-flash-lite-latest").strip()
CORTEX_TIMEOUT = int(os.getenv("CORTEX_TIMEOUT", "15"))
CORTEX_MAX_SUMMARY_TOKENS = int(os.getenv("CORTEX_MAX_SUMMARY_TOKENS", "350"))
FETCH_TIMEOUT = int(os.getenv("CORTEX_FETCH_TIMEOUT", "10"))
FETCH_MAX_BYTES = int(os.getenv("CORTEX_FETCH_MAX_BYTES", "400000"))  # 400KB cukup
POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"

# =========================================================================
#  TOOL 1: KALKULATOR
# =========================================================================

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_ALLOWED_UNARY = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}
_ALLOWED_FUNCS = {
    "akar": math.sqrt, "sqrt": math.sqrt,
    "pangkat": pow, "pow": pow,
    "abs": abs, "bulat": round, "round": round,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log10, "ln": math.log,
}
_ALLOWED_CONSTS = {"pi": math.pi, "e": math.e}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("konstanta non-angka")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if not op:
            raise ValueError("operator gak didukung")
        kiri, kanan = _safe_eval(node.left), _safe_eval(node.right)
        # Pangkat raksasa bisa hang — cap eksponen.
        if isinstance(node.op, ast.Pow) and abs(kanan) > 1000:
            raise ValueError("pangkat kebesaran")
        return op(kiri, kanan)
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARY.get(type(node.op))
        if not op:
            raise ValueError("operator unary gak didukung")
        return op(_safe_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("fungsi aneh")
        fn = _ALLOWED_FUNCS.get(node.func.id.lower())
        if not fn:
            raise ValueError(f"fungsi '{node.func.id}' gak ada")
        args = [_safe_eval(a) for a in node.args]
        return fn(*args)
    if isinstance(node, ast.Name):
        if node.id.lower() in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id.lower()]
        raise ValueError(f"variabel '{node.id}' gak dikenal")
    raise ValueError("ekspresi gak valid")


# Normalisasi gaya Indonesia: "12,5" -> 12.5, "10.000" -> 10000 itu ambigu,
# jadi cuma dukung koma desimal kalau polanya jelas desimal.
_CANDIDATE_RE = re.compile(
    r"(?:berapa\s+)?(?:hitung\s+)?"
    r"([\-+]?\(?\s*[\d][\d\s+\-*/().,%^xX:]*[\d)\s])"
    r"\s*(?:=+\s*\??|\?)?\s*$"
)


def try_calculator(teks: str):
    """Balikin string jawaban kalau teks = soal hitungan, else None."""
    t = teks.strip().lower()
    if len(t) > 120:
        return None
    # Jalur persen: "17% dari 200", "berapa 20 persen dari 150" -> "200 * 17 / 100"
    mp = re.match(r"(?:berapa\s+)?(\d+(?:[.,]\d+)?)\s*(?:%|persen)\s*dari\s*(\d+(?:[.,]\d+)?)\s*\??$", t)
    if mp:
        persen = mp.group(1).replace(",", ".")
        nilai = mp.group(2).replace(",", ".")
        try:
            pohon = ast.parse(f"{nilai} * {persen} / 100", mode="eval")
            hasil_p = _safe_eval(pohon)
            tampil_p = f"{hasil_p:,}".replace(",", ".") if abs(hasil_p) >= 10000 else f"{hasil_p:g}"
            return f"�� **{mp.group(1)}% dari {mp.group(2)}** = **{tampil_p}**"
        except Exception:
            return None
    # Jalur fungsi: "akar 144" -> "akar(144)", "pangkat 2 10" -> "pangkat(2, 10)"
    mf = re.match(r"(?:berapa\s+)?(akar|sqrt|pangkat|pow|sin|cos|tan|log|ln)\s+((?:\d+(?:[.,]\d+)?\s*)+)\s*\??$", t)
    pakai_fungsi = False
    if mf:
        fn = mf.group(1)
        args = ", ".join(mf.group(2).split())
        t = f"{fn}({args})"
        pakai_fungsi = True
    if pakai_fungsi:
        expr = t
        tampil_input = t
    else:
        m = _CANDIDATE_RE.match(t)
        if not m:
            return None
        expr = m.group(1).strip()
        tampil_input = m.group(1).strip()
    # Harus ada operator ATAU nama fungsi — kalau cuma angka polos, bukan soal.
    if not re.search(r"[+\-*/%^xX:]", expr) and not re.search(
            r"\b(akar|sqrt|pangkat|pow|sin|cos|tan|log|ln)\b", expr):
        return None
    # Normalisasi: x/X/: jadi operator standar, ^ jadi **, koma desimal jadi titik.
    expr = expr.replace("x", "*").replace(":", "/").replace("^", "**")
    if not pakai_fungsi:  # koma di jalur fungsi = pemisah argumen, jangan diutak-atik
        expr = re.sub(r"(\d),(\d)", r"\1.\2", expr)  # 12,5 -> 12.5
        expr = expr.replace(",", "")  # buang sisa koma ribuan
    try:
        tree = ast.parse(expr, mode="eval")
        hasil = _safe_eval(tree)
    except ZeroDivisionError:
        return "♾️ Hasilnya tak terhingga bro (dibagi nol)."
    except Exception:
        return None  # gagal parse = bukan soal matematika, lempar ke AI
    if isinstance(hasil, float) and hasil == int(hasil) and abs(hasil) < 1e15:
        hasil = int(hasil)
    if isinstance(hasil, (int, float)):
        # Pemisah ribuan cuma kalau >= 10000 — 1.024 utk 1024 ambigu kayak desimal.
        if abs(hasil) < 10000:
            tampil = f"{hasil:g}" if isinstance(hasil, float) else str(hasil)
        else:
            tampil = f"{hasil:,}".replace(",", ".")
    else:
        tampil = str(hasil)
    return f"�� **{tampil_input}** = **{tampil}**"


# =========================================================================
#  TOOL 2: BACA LINK (fetch + rangkum via Gemini)
# =========================================================================

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3",
             ".zip", ".rar", ".7z", ".pdf", ".apk", ".exe")


def detect_url(teks: str):
    """Ambil URL pertama yang layak dibaca dari teks."""
    for u in _URL_RE.findall(teks or ""):
        u = u.rstrip(".,!?)")
        path = u.split("?")[0].lower()
        if not any(path.endswith(e) for e in _SKIP_EXT):
            return u
    return None


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|header|footer|nav|aside|form)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;?", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"&#39;|&rsquo;|&lsquo;", "'", html)
    html = re.sub(r"&quot;|&ldquo;|&rdquo;", '"', html)
    return re.sub(r"\s+", " ", html).strip()


def fetch_page_text(url: str):
    """Ambil isi halaman sebagai teks polos. Balikin (teks, error)."""
    try:
        r = requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0 Safari/537.36"},
        )
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text" not in ctype and "json" not in ctype and "html" not in ctype:
            return None, f"bukan halaman teks ({ctype.split(';')[0]})"
        chunks, total = [], 0
        for chunk in r.iter_content(8192, decode_unicode=True):
            chunks.append(chunk)
            total += len(chunk or "")
            if total >= FETCH_MAX_BYTES:
                break
        r.close()
        mentah = "".join(chunks)
        return _strip_html(mentah)[:12000], None
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError:
        return None, "koneksi gagal"
    except Exception as e:
        return None, str(e)[:60]


def _gemini_summarize(teks_halaman: str, url: str, pertanyaan: str) -> str:
    """Rangkum isi halaman pakai Gemini flash-lite (cepat & murah)."""
    if not GEMINI_API_KEY:
        return ""
    prompt = (
        "Rangkum isi halaman web berikut dalam bahasa Indonesia santai, "
        "maksimal 4 kalimat. Fokus ke inti informasinya. "
        + (f"User bertanya soal: \"{pertanyaan}\" — jawab juga itu kalau bisa. " if pertanyaan else "")
        + f"\n\nURL: {url}\nISI HALAMAN:\n{teks_halaman[:9000]}"
    )
    gdata = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": CORTEX_MAX_SUMMARY_TOKENS},
    }
    try:
        res = requests.post(
            f"{GEMINI_BASE_URL}/models/{CORTEX_GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json=gdata,
            timeout=CORTEX_TIMEOUT,
        )
        data = res.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip()
    except Exception as e:
        logger.warning(f"Cortex summarize gagal: {e}")
        return ""


def try_read_link(teks: str):
    """Kalau ada URL di pesan -> baca & rangkum. Balikin (jawaban, url) atau None."""
    url = detect_url(teks)
    if not url:
        return None
    isi, err = fetch_page_text(url)
    if isi is None:
        return (f"🔗 Gak bisa buka link itu bro ({err}).", url)
    # Ambang tipis = 60 char. Halaman valid sering pendek (example.com = 142
    # char) — ambang 200 bikin halaman sah dituduh "butuh login".
    if len(isi) < 60:
        return ("�� Halamannya kebuka tapi teksnya kosong — kemungkinan butuh "
                "login atau isinya di-render pakai JS. Coba jelasin aja isinya apa.", url)
    pertanyaan = _URL_RE.sub("", teks).strip()
    ringkas = _gemini_summarize(isi, url, pertanyaan)
    if not ringkas:
        # Fallback tanpa Gemini: kasih potongan awal halaman.
        ringkas = isi[:400] + "..."
    return (f"🔗 **Rangkuman dari link lu:**\n{ringkas}", url)


# =========================================================================
#  TOOL 3: BIKIN GAMBAR (Pollinations.ai — gratis, tanpa key)
# =========================================================================

_IMG_TRIGGERS = (
    "gambar ", "gambarkan ", "gambarin ", "bikin gambar", "buatkan gambar",
    "buat gambar", "lukis ", "generate gambar", "corteks gambar",
)


def try_make_image(teks: str):
    """Kalau user minta gambar -> balikin (url_gambar, prompt) atau None."""
    t = (teks or "").strip().lower()
    prompt = None
    for trig in _IMG_TRIGGERS:
        idx = t.find(trig)
        if idx != -1:
            prompt = teks[idx + len(trig):].strip(" .!")
            break
    if not prompt or len(prompt) < 3:
        return None
    if len(prompt) > 300:
        prompt = prompt[:300]
    seed = int(time.time()) % 100000
    encoded = urllib.parse.quote(prompt, safe="")
    url = f"{POLLINATIONS_BASE}{encoded}?width=1024&height=1024&seed={seed}&nologo=true"
    return (url, prompt)


# =========================================================================
#  ROUTER UTAMA
# =========================================================================

async def cortex_handle(pertanyaan: str):
    """
    Cek apakah pesan ini tugas tools Cortex.
    Return: dict {kind, text, image_url?} atau None (bukan tugas tools).
    Urutan penting: link dulu (pesan berisi URL + minta rangkum), lalu
    kalkulator, terakhir gambar (trigger paling eksplisit).
    """
    try:
        # Kalkulator — paling murah, cek duluan.
        hasil_calc = try_calculator(pertanyaan)
        if hasil_calc:
            return {"kind": "calc", "text": hasil_calc}

        # Gambar — trigger eksplisit.
        img = try_make_image(pertanyaan)
        if img:
            url, prompt = img
            return {"kind": "image", "text": f"🎨 Nih gambarnya: **{prompt}**",
                    "image_url": url}

        # Link — butuh network, taruh terakhir.
        link = try_read_link(pertanyaan)
        if link:
            jawaban, _url = link
            return {"kind": "link", "text": jawaban}

        return None
    except Exception as e:
        logger.error(f"Cortex error: {e}")
        return None  # gagal = fallback ke jalur AI biasa, jangan ganggu user
