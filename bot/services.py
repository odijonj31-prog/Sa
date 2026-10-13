import asyncio
import html as _html
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import config

try:
    from shazamio import Shazam
except Exception:  # pragma: no cover
    Shazam = None

log = logging.getLogger(__name__)

JOBS = asyncio.Semaphore(config.MAX_JOBS)
FF = asyncio.Semaphore(config.FFMPEG_JOBS)
RSEM = asyncio.Semaphore(config.RESOLVE_JOBS)
SSEM = asyncio.Semaphore(8)
_shazam = None


class Skipped(Exception):
    """Yuklash ataylab o'tkazib yuborildi: reason = 'long' | 'live'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Result:
    files: list[Path] = field(default_factory=list)
    title: str = ""


@dataclass
class Song:
    path: Path
    title: str
    artist: str


def tmp(ext: str, prefix: str = "f") -> Path:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    return config.TMP_DIR / f"{prefix}_{uuid.uuid4().hex[:10]}{ext}"


# ---------------- ffmpeg ----------------
async def ffmpeg(*args: str, timeout: int = 300) -> bool:
    async with FF:
        return await _ffmpeg(*args, timeout=timeout)


async def _ffmpeg(*args: str, timeout: int = 300) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    if proc.returncode != 0:
        log.warning("ffmpeg xato: %s", err.decode(errors="ignore")[-400:])
    return proc.returncode == 0


async def make_circle(src: Path) -> Path | None:
    out = tmp(".mp4", "circle")
    ok = await ffmpeg(
        "-i", str(src), "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640,setsar=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out))
    return out if ok and out.exists() else None


async def to_mp3(src: Path) -> Path | None:
    out = tmp(".mp3", "audio")
    ok = await ffmpeg("-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(out))
    return out if ok and out.exists() else None


async def shrink(src: Path) -> Path | None:
    """Katta videoni 480p ga kichraytiradi. Sig'masa None."""
    out = tmp(".mp4", "small")
    ok = await ffmpeg(
        "-i", str(src), "-vf", "scale=-2:'min(480,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out), timeout=900)
    if ok and out.exists() and out.stat().st_size <= config.MAX_UPLOAD:
        return out
    out.unlink(missing_ok=True)
    return None


# ---------------- Shazam ----------------
async def recognize(src: Path) -> dict | None:
    global _shazam
    if Shazam is None:
        return None
    if _shazam is None:
        _shazam = Shazam()
    sample = tmp(".mp3", "sample")
    try:
        if not await ffmpeg("-i", str(src), "-vn", "-t", "30", "-ac", "1", "-ar", "44100", str(sample)):
            return None
        fn = getattr(_shazam, "recognize", None) or getattr(_shazam, "recognize_song")
        res = await fn(str(sample))
    finally:
        sample.unlink(missing_ok=True)
    track = (res or {}).get("track")
    if not track:
        return None
    return {
        "title": track.get("title", "?"),
        "artist": track.get("subtitle", "?"),
        "url": track.get("url"),
        "cover": (track.get("images") or {}).get("coverarthq") or (track.get("images") or {}).get("coverart"),
    }


# ---------------- yt-dlp ----------------
YT_HOSTS = ("youtube.com", "youtu.be")
IMG_EXT = {"jpg", "jpeg", "png", "webp"}
NO_SCRAPE = ("instagram.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be", "tiktok.com")
YT_CLIENTS = [["android_vr"], ["tv"], None]   # None = standart
_best = [0]
_yt_block_until = [0.0]   # YouTube bloklasa, shu vaqtgacha urinmaymiz (sekinlashishning oldini oladi)


def yt_blocked() -> bool:
    return time.monotonic() < _yt_block_until[0]


def yt_auth() -> bool:
    """YouTube uchun cookies yoki proksi sozlanganmi."""
    return bool(config.PROXY_URL or (config.COOKIES_FILE and Path(config.COOKIES_FILE).exists()))


def is_youtube(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in YT_HOSTS) or u.startswith("ytsearch")


def _ig_public(url: str) -> bool:
    """Instagram post/reels (stories emas): cookies'siz ishlaydi, cookies bilan esa 400 xato berishi mumkin."""
    u = url.lower()
    return "instagram.com" in u and "/stories/" not in u


def needs_proxy(url: str) -> bool:
    """Proksi faqat kerakli joyda: YouTube va Instagram stories. Qolganlari (SoundCloud, reels...) proksisiz tez va bepul."""
    u = url.lower()
    return is_youtube(u) or "/stories/" in u


def _base(clients=None, cookies: bool = True, proxy: bool = False) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True,
         "socket_timeout": 20, "retries": 2, "noplaylist": True}
    if cookies and config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():
        o["cookiefile"] = config.COOKIES_FILE
    if proxy and config.PROXY_URL:
        o["proxy"] = config.PROXY_URL
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    return o


def _with_clients(target: str, fn):
    """YouTube 'bot' tekshiruvi chiqsa, boshqa klientlar bilan urinib ko'radi."""
    if not is_youtube(target):
        return fn(None)
    if yt_blocked():
        raise DownloadError("YouTube vaqtincha bloklangan (kutish rejimi)")
    n = len(YT_CLIENTS)
    last = None
    for k in range(n):
        idx = (_best[0] + k) % n
        try:
            res = fn(YT_CLIENTS[idx])
            _best[0] = idx
            _yt_block_until[0] = 0.0
            return res
        except Skipped:
            raise
        except DownloadError as e:
            last = e
            msg = str(e).lower()
            if "sign in" not in msg and "bot" not in msg and "not available" not in msg:
                raise
            log.info("YouTube klient %s o'tmadi", YT_CLIENTS[idx])
    wait = 600 if yt_auth() else 3600   # cookies/proksi bo'lmasa, tez-tez urinish foydasiz: har urinish foydalanuvchini ~6 s kutdiradi
    _yt_block_until[0] = time.monotonic() + wait
    log.warning("YouTube bloklangan: %d daqiqa SoundCloud'ga o'tildi (cookies/proksi: %s)", wait // 60, yt_auth())
    raise last


def video_format(h: int) -> str:
    return (f"bv*[height<={h}][vcodec^=avc]+ba[acodec^=mp4a]"
            f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
            f"/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b")


def _download_once(url: str, height: int, clients, max_items: int = 10, cookies: bool = True,
                   proxy: bool | None = None) -> Result:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = uuid.uuid4().hex[:8]
    skip: dict[str, str] = {}

    def flt(info, *, incomplete):
        if info.get("is_live"):
            skip["r"] = "live"
            return "live"
        d = info.get("duration")
        if d and d > config.MAX_VIDEO_SEC:
            skip["r"] = "long"
            return "long"
        return None

    opts = _base(clients, cookies, needs_proxy(url) if proxy is None else proxy) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}_%(id).50s.%(ext)s"),
        "format": video_format(height), "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4, "http_chunk_size": 10485760,
        "playlist_items": f"1-{max_items}", "match_filter": flt,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    found: list[Path] = []
    entries = (info or {}).get("entries")
    for e in (list(entries) if entries else [info]):
        for d in (e or {}).get("requested_downloads") or []:
            p = Path(d.get("filepath", ""))
            if p.is_file() and p not in found:
                found.append(p)
    if not found:
        found = sorted(config.TMP_DIR.glob(f"{prefix}_*"))
    bad = {".part", ".ytdl", ".json", ".vtt", ".temp"}
    found = [p for p in found if p.suffix.lower() not in bad]
    if not found and skip:
        raise Skipped(skip["r"])
    return Result(found, (info or {}).get("title") or "")


# ---------------- Instagram rasmli postlar: embed sahifasidan ----------------
_IG_CODE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)", re.I)


def _http_get(url: str, proxy: str | None = None, timeout: int = 15) -> bytes:
    import urllib.request
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return r.read(3_000_000)


def _ig_embed_urls(html_text: str) -> list[str]:
    """Embed sahifasidagi rasm havolalari (JSON ichida ham, <img> ichida ham)."""
    found: list[str] = []
    for m in re.finditer(r'\\?"display_url\\?"\s*:\s*\\?"(.+?)\\?"\s*[,}]', html_text):
        found.append(m.group(1))
    for m in re.finditer(r'class="[^"]*EmbeddedMediaImage[^"]*"[^>]*?src="([^"]+)"', html_text):
        found.append(m.group(1))
    out, seen = [], set()
    for u in found:
        u = u.replace("\\\\u0026", "&").replace("\\u0026", "&").replace("\\\\/", "/").replace("\\/", "/")
        u = _html.unescape(u)
        if not u.startswith("http"):
            continue
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out[:10]


def _ig_photos(url: str) -> Result | None:
    """yt-dlp rasmli postni (video yo'q) yuklay olmasa: embed sahifasidan rasmlarni olamiz. Sinalmagan zaxira yo'l."""
    m = _IG_CODE.search(url)
    if not m:
        return None
    code = m.group(1)
    for proxy in ([None, config.PROXY_URL] if config.PROXY_URL else [None]):
        try:
            page = _http_get(f"https://www.instagram.com/p/{code}/embed/captioned/", proxy).decode("utf-8", "ignore")
            urls = _ig_embed_urls(page)
            if not urls:
                continue
            files = []
            for i, u in enumerate(urls):
                data = _http_get(u, proxy)
                if len(data) < 2000:
                    continue
                f = config.TMP_DIR / f"igp_{uuid.uuid4().hex[:6]}_{i}.jpg"
                f.write_bytes(data)
                files.append(f)
            if files:
                return Result(files, "")
        except Exception as e:
            log.info("Instagram embed o'tmadi (proksi=%s): %s", bool(proxy), str(e)[:120])
    return None


def _download(url: str, height: int = 720, max_items: int = 10) -> Result:
    if _ig_public(url):
        err = None
        try:   # 1) cookies'siz, proksisiz (tez va ishonchli)
            return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, False, False))
        except Skipped:
            raise
        except Exception as e:
            err = e
            log.info("Instagram cookies'siz o'tmadi: %s", str(e)[:120])
        if config.PROXY_URL:   # 2) Instagram datacenter IP'ni bloklasa: proksi orqali
            try:
                return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, False, True))
            except Skipped:
                raise
            except Exception as e:
                err = e
                log.info("Instagram proksi bilan o'tmadi: %s", str(e)[:120])
        if "No video formats" in str(err) or "empty media" in str(err):   # 3) rasmli post
            res = _ig_photos(url)
            if res:
                return res
        try:   # 4) oxirgi urinish: cookies bilan
            return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, True, False))
        except Exception:
            raise err
    return _with_clients(url, lambda cl: _download_once(url, height, cl, max_items, True))


async def download(url: str, height: int = 720, max_items: int = 10) -> Result:
    async with JOBS:
        return await asyncio.to_thread(_download, url, height, max_items)


# ---------------- tezkor yo'l: to'g'ridan-to'g'ri havola ----------------
def _resolve(url: str) -> dict | None:
    """Faylni yuklamasdan, Telegram o'zi olishi mumkin bo'lgan havolani topadi."""
    opts = _base(cookies=not _ig_public(url), proxy=needs_proxy(url)) | {"format": "b[ext=mp4][height<=720]/b[height<=720]/b", "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or info.get("entries") or info.get("is_live") or info.get("requested_formats"):
        return None
    d = info.get("duration")
    if d and d > config.MAX_VIDEO_SEC:
        raise Skipped("long")
    link = info.get("url")
    if not link or not link.startswith("http") or ".m3u8" in link:
        return None
    size = info.get("filesize") or info.get("filesize_approx")
    if size and size > config.MAX_DOWNLOAD_FROM_TG:
        return None
    ext = (info.get("ext") or "").lower()
    return {"url": link, "kind": "photo" if ext in IMG_EXT else "video", "title": info.get("title") or ""}


async def resolve(url: str) -> dict | None:
    async with RSEM:
        return await asyncio.to_thread(_resolve, url)


# ---------------- zaxira: sahifadagi og:video / og:image ----------------
async def scrape_media(url: str) -> dict | None:
    if any(h in url.lower() for h in NO_SCRAPE):
        return None
    import aiohttp
    hdr = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=hdr) as s:
        async with s.get(url, allow_redirects=True) as r:
            if r.status != 200:
                return None
            page = (await r.content.read(700_000)).decode("utf-8", "ignore")

    def meta(prop: str):
        pr = re.escape(prop)
        m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)' % pr, page, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']' % pr, page, re.I))
        return _html.unescape(m.group(1)) if m else None

    v = meta("og:video:secure_url") or meta("og:video:url") or meta("og:video")
    if v and v.startswith("http"):
        return {"url": v, "kind": "video", "title": meta("og:title") or ""}
    i = meta("og:image")
    if i and i.startswith("http"):
        return {"url": i, "kind": "photo", "title": meta("og:title") or ""}
    return None


# ---------------- qo'shiq: yuklash va qidirish ----------------
def _song_opts(prefix: str, clients=None, proxy: bool = False) -> dict:
    def flt(info, *, incomplete):
        d = info.get("duration")
        return "long" if d and d > config.MAX_SONG_SEC else None

    return _base(clients, proxy=proxy) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}.%(ext)s"),
        # m4a/mp3 bo'lsa o'zgartirmasdan yuboriladi (konvertatsiya 1-3 soniya oladi)
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "match_filter": flt,
    }


def _finish_song(prefix: str, entry: dict) -> Song | None:
    files = sorted(config.TMP_DIR.glob(f"{prefix}.*"))
    ok = [p for p in files if p.suffix.lower() in (".m4a", ".mp3")]
    if not ok and files:   # Telegram faqat mp3/m4a qabul qiladi: boshqa format bo'lsa o'giramiz
        out = config.TMP_DIR / f"{prefix}.mp3"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(files[0]), "-vn",
                            "-c:a", "libmp3lame", "-b:a", "192k", str(out)], capture_output=True, timeout=180)
        if r.returncode == 0 and out.exists():
            ok = [out]
    for p in files:
        if p not in ok:
            p.unlink(missing_ok=True)
    if not ok:
        return None
    return Song(ok[0], entry.get("track") or entry.get("title") or "Audio",
                entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")


def _song_once(target: str, clients) -> Song | None:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]
    with YoutubeDL(_song_opts(prefix, clients, needs_proxy(target))) as ydl:
        info = ydl.extract_info(target, download=True)
    entries = (info or {}).get("entries")
    entry = (list(entries)[0] if entries else info) or {}
    return _finish_song(prefix, entry)


def _clean_query(q: str) -> str:
    """'Nom (Official Video) #tag' -> 'Nom': SoundCloud'da yaxshiroq topiladi."""
    q = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", q)
    q = re.sub(r"#\w+", " ", q)
    q = re.sub(r"(?i)\b(official|music|video|audio|lyrics|lyric|hd|4k|full|mv|clip|klip|premyera|yangi)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip(" -–|")


def _sc_download(q: str) -> Song | None:
    """SoundCloud: DRM bilan himoyalangan natijalarni o'tkazib, birinchi yuklanadiganini oladi."""
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]
    got: dict = {}

    def hook(d):
        if d.get("status") == "finished" and not got:
            got.update(d.get("info_dict") or {})

    opts = _song_opts(prefix) | {"ignoreerrors": True, "max_downloads": 1, "playlist_items": "1-6",
                                 "progress_hooks": [hook]}
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([f"scsearch6:{q}"])
    except Exception as e:
        log.info("SoundCloud xato: %s", str(e)[:150])
    return _finish_song(prefix, got)


def _song(target: str, title: str | None) -> Song | None:
    try:
        song = _with_clients(target, lambda cl: _song_once(target, cl))
        if song:
            return song
    except Exception as e:
        log.info("Qo'shiq asosiy manbadan olinmadi (%s): %s", target, str(e)[:160])
    # zaxira: SoundCloud (datacenter IP'larni bloklamaydi)
    q = title or (target.split(":", 1)[1] if target.startswith("ytsearch") else None)
    if not q:
        return None
    cleaned = _clean_query(q)
    song = _sc_download(cleaned) if cleaned else None
    if not song and cleaned != q:
        song = _sc_download(q)
    return song


async def download_song(target: str, title: str | None = None) -> Song | None:
    async with JOBS:
        t0 = time.monotonic()
        song = await asyncio.to_thread(_song, target, title)
        log.info("TIMING yuklash %.1fs ok=%s %r", time.monotonic() - t0, bool(song), (title or target)[:40])
        return song


def _search_flat(prefix: str, q: str, n: int) -> list[dict]:
    opts = _base(proxy=(prefix == "ytsearch")) | {"extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{q}", download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        d = int(e.get("duration") or 0)
        if d > config.MAX_SONG_SEC:
            continue
        link = e.get("url") or e.get("webpage_url")
        if prefix == "ytsearch":
            if not e.get("id"):
                continue
            link = f"https://www.youtube.com/watch?v={e['id']}"
        if not link:
            continue
        out.append({"target": link, "title": e.get("title") or "?", "duration": d})
    return out


_SC: dict[str, tuple[float, list]] = {}


async def _one(prefix: str, q: str, n: int) -> list[dict]:
    async with SSEM:
     
