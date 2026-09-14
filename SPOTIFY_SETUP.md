# Seraphine Bot — Spotify Integration

**Status:** Kode live di Railway (deployment `278b791a`), TAPI butuh kredensial Spotify Notzee.

## Yang sudah dikerjakan

`/splay` sekarang nerima:
- URL YouTube / judul lagu (kayak sebelumnya)
- **Link Spotify**: `https://open.spotify.com/track/...`, `/playlist/...`, `/album/...` (plus link `intl-id/track/...`)
- **Query `spotify: <judul lagu>`** → pakai Spotify Search API

Cara kerja: metadata diambil dari **Spotify Web API (Client Credentials Flow)**,
terus audionya diputar lewat YouTube search (yt-dlp). Ini karena Spotify gak
ngasih audio streaming buat bot gratisan.

Kalau playlist/album: lagu pertama langsung diputar, sisanya (sampai 50 lagu) masuk `/squeue`.

## LANGKAH WAJIB NOTZEE (belum selesai)

Fitur ini **belum aktif** karena `SPOTIFY_CLIENT_ID` & `SPOTIFY_CLIENT_SECRET` belum ada.

1. Buka https://developer.spotify.com/dashboard → Login (akun Spotify biasa) → **Create app**
2. Isi nama app bebas (mis. "Seraphine Bot"), Redirect URI isi `http://localhost:8080` (gak dipakai tapi wajib)
3. Copy **Client ID** & **Client Secret**
4. Set di Railway:
   ```bash
   cd "C:/Users/Elica/seraphine-bot-openrouter"
   railway variables --set "SPOTIFY_CLIENT_ID=xxxx"
   railway variables --set "SPOTIFY_CLIENT_SECRET=yyyy"
   railway variables --json   # verifikasi keduanya masuk
   railway redeploy --from-source --yes
   ```

Kalau belum diisi, `/splay <link spotify>` bakal bales pesan error yang ngasih tau Notzee buat setup dulu (bukan crash).

## File yang diubah
- `bot.py` — modul Spotify (resolver + token cache) & integrasi `/splay`
- `.env.example` — tambah `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`

## Catatan teknis
- Token Spotify di-cache sampe expired (`_SPOTIFY_TOKEN_CACHE`), auto-refresh kalau 401.
- Resolver jalan di `run_in_executor` biar gak nge-block event loop.
- Limit playlist/album = 50 lagu pertama (biar antrean gak kebanjiran).
