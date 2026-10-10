import asyncio
import html as _html
import logging
import re
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


def is_youtube(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in YT_HOSTS) or u.startswith("ytsearch")


def _base(clients=None) -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True,
         "socket_timeout": 20, "retries": 2, "noplaylist": True}
    if config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():
        o["cookiefile"] = config.COOKIES_FILE
    if config.PROXY_URL:
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
    _yt_block_until[0] = time.monotonic() + 600
    log.warning("YouTube bloklangan: 10 daqiqa SoundCloud'ga o'tildi")
    raise last


def video_format(h: int) -> str:
    return (f"bv*[height<={h}][vcodec^=avc]+ba[acodec^=mp4a]"
            f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
            f"/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b")


def _download_once(url: str, height: int, clients) -> Result:
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

    opts = _base(clients) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}_%(id).50s.%(ext)s"),
        "format": video_format(height), "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4, "http_chunk_size": 10485760,
        "playlist_items": "1-10", "match_filter": flt,
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


def _download(url: str, height: int = 720) -> Result:
    return _with_clients(url, lambda cl: _download_once(url, height, cl))


async def download(url: str, height: int = 720) -> Result:
    async with JOBS:
        return await asyncio.to_thread(_download, url, height)


# ---------------- tezkor yo'l: to'g'ridan-to'g'ri havola ----------------
def _resolve(url: str) -> dict | None:
    """Faylni yuklamasdan, Telegram o'zi olishi mumkin bo'lgan havolani topadi."""
    opts = _base() | {"format": "b[ext=mp4][height<=720]/b[height<=720]/b", "skip_download": True}
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
def _song_once(target: str, clients) -> Song | None:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = "song_" + uuid.uuid4().hex[:8]

    def flt(info, *, incomplete):
        d = info.get("duration")
        return "long" if d and d > config.MAX_SONG_SEC else None

    opts = _base(clients) | {
        "outtmpl": str(config.TMP_DIR / f"{prefix}.%(ext)s"),
        "format": "bestaudio/best", "match_filter": flt,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=True)
    entries = (info or {}).get("entries")
    entry = (list(entries)[0] if entries else info) or {}
    files = sorted(config.TMP_DIR.glob(f"{prefix}.*"))
    mp3 = [p for p in files if p.suffix == ".mp3"]
    for p in files:
        if p not in mp3:
            p.unlink(missing_ok=True)
    if not mp3:
        return None
    return Song(mp3[0], entry.get("track") or entry.get("title") or "Audio",
                entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")


def _song(target: str, title: str | None) -> Song | None:
    try:
        song = _with_clients(target, lambda cl: _song_once(target, cl))
        if song:
            return song
    except Exception as e:
        log.info("Qo'shiq asosiy manbadan olinmadi (%s): %s", target, str(e)[:160])
    # zaxira: SoundCloud (datacenter IP'larni bloklamaydi)
    q = title or (target.split(":", 1)[1] if target.startswith("ytsearch") else None)
    if q:
        return _song_once(f"scsearch1:{q}", None)
    return None


async def download_song(target: str, title: str | None = None) -> Song | None:
    async with JOBS:
        return await asyncio.to_thread(_song, target, title)


def _search_flat(prefix: str, q: str, n: int) -> list[dict]:
    opts = _base() | {"extract_flat": True, "skip_download": True}
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


async def search(q: str, n: int = 20) -> list[dict]:
    key = q.lower().strip()
    hit = _SC.get(key)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    # YouTube bloklangan bo'lsa, yuklab bo'ladigan SoundCloud birinchi qidiriladi
    order = ("scsearch", "ytsearch") if yt_blocked() else ("ytsearch", "scsearch")
    async with SSEM:
        for prefix in order:
            try:
                res = await asyncio.to_thread(_search_flat, prefix, q, n)
            except Exception as e:
                log.info("%s qidiruvi xato: %s", prefix, str(e)[:160])
                continue
            if res:
                if len(_SC) > 3000:
                    _SC.clear()
                _SC[key] = (time.monotonic(), res)
                return res
    return []


# ---------------- ovozni matnga o'girish ----------------
async def _groq(src: Path) -> str:
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("file", src.read_bytes(), filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", "whisper-large-v3-turbo")
    form.add_field("response_format", "json")
    form.add_field("temperature", "0")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post("https://api.groq.com/openai/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}, data=form) as r:
            if r.status != 200:
                log.warning("Groq xato %s: %s", r.status, (await r.text())[:200])
                return ""
            return ((await r.json()).get("text") or "").strip()


def _google_stt(wav: Path, lang: str) -> str:
    try:
        import speech_recognition as sr
    except Exception:
        return ""
    rec = sr.Recognizer()
    with sr.AudioFile(str(wav)) as s:
        audio = rec.record(s)
    order = {"uz": ["uz-UZ", "ru-RU", "en-US"], "ru": ["ru-RU", "uz-UZ", "en-US"]}.get(lang, ["en-US", "ru-RU", "uz-UZ"])
    for code in order:
        try:
            return rec.recognize_google(audio, language=code).strip()
        except sr.UnknownValueError:
            continue
        except Exception as e:
            log.warning("Google STT xato: %s", str(e)[:150])
            return ""
    return ""


async def transcribe(src: Path, lang: str) -> str:
    if config.GROQ_API_KEY:
        try:
            text = await _groq(src)
            if text:
                return text
        except Exception as e:
            log.warning("Groq istisno: %s", str(e)[:150])
    wav = tmp(".wav", "stt")
    try:
        if not await ffmpeg("-i", str(src), "-ac", "1", "-ar", "16000", str(wav), timeout=60):
            return ""
        return await asyncio.to_thread(_google_stt, wav, lang)
    finally:
        wav.unlink(missing_ok=True)


async def make_gif(src: Path) -> Path | None:
    """Videoning boshidan 6 soniya, ovozsiz (Telegram animatsiyasi)."""
    out = tmp(".mp4", "gif")
    ok = await ffmpeg("-i", str(src), "-t", "6", "-an", "-vf", "scale='min(480,iw)':-2,fps=20",
                      "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
                      "-movflags", "+faststart", str(out))
    return out if ok and out.exists() else None


async def make_cover(src: Path) -> Path | None:
    out = tmp(".jpg", "cover")
    for ss in ("1", "0"):
        if await ffmpeg("-ss", ss, "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)) and out.exists():
            return out
    return None
