import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

TMP_ROOT = Path(tempfile.gettempdir()) / "neuroslop-downloads"
YTDLP_BIN = shutil.which("yt-dlp") or "yt-dlp"

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_DURATION_SECONDS = 600
MAX_CONCURRENT = 2
DOWNLOAD_TIMEOUT = 180

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}

FORMATS = [
    "bv*[height<=1080]+ba/b[height<=1080]",
    "b[height<=720]",
    "b[height<=480]",
    "b",
]

VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
_MEDIA_IGNORE = (".info.json", ".part", ".ytdl", ".temp")

_active_urls: set[str] = set()
_semaphores: dict = {}


class LinkError(Exception):
    code = "link_error"

    def __init__(self, message="", detail=""):
        super().__init__(message or self.code)
        self.detail = detail


class InvalidLink(LinkError):
    code = "invalid"


class UnsupportedSource(LinkError):
    code = "unsupported"


class LinkBusy(LinkError):
    code = "busy"


class DownloadFailed(LinkError):
    code = "failed"


class DownloadTimeout(DownloadFailed):
    code = "timeout"


class TooLarge(DownloadFailed):
    code = "too_large"


class TooLong(DownloadFailed):
    code = "too_long"


@dataclass
class DownloadResult:
    path: Path
    size: int
    duration: int | None
    url: str
    meta: dict


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        _semaphores[loop] = semaphore
    return semaphore


def cleanup_stale():
    """Удаляет только каталог загрузчика."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    for child in TMP_ROOT.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            log.warning("cannot clean stale download item %s", child)


def cleanup(result: DownloadResult | None):
    if result is None:
        return
    shutil.rmtree(result.path.parent, ignore_errors=True)


def _video_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,}", value))


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or len(raw) > 2048:
        raise InvalidLink("empty or too long")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise InvalidLink("scheme")
    if parsed.username or parsed.password:
        raise InvalidLink("credentials")
    if parsed.port not in (None, 80, 443):
        raise InvalidLink("port")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UnsupportedSource("host")

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if not _video_id(video_id):
            raise InvalidLink("youtube id")
        return f"https://www.youtube.com/watch?v={video_id}"

    if "youtube.com" in host:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            parts = parsed.path.strip("/").split("/")
            video_id = parts[1] if len(parts) > 1 else ""
        else:
            raise InvalidLink("unsupported youtube path")
        if not _video_id(video_id):
            raise InvalidLink("youtube id")
        return f"https://www.youtube.com/watch?v={video_id}"

    match = re.match(r"^/(reel|reels|p|tv)/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise InvalidLink("unsupported instagram path")
    kind = "reel" if match.group(1) in ("reel", "reels") else match.group(1)
    return f"https://www.instagram.com/{kind}/{match.group(2)}/"


def build_meta(info: dict) -> dict:
    tags = [str(item)[:50] for item in (info.get("tags") or [])][:30]
    return {
        "title": (info.get("title") or "").strip()[:300],
        "author": (info.get("uploader") or info.get("channel") or "").strip()[:200],
        "description": (info.get("description") or "").strip()[:2000],
        "tags": tags,
        "webpage_url": info.get("webpage_url") or "",
        "duration": info.get("duration"),
        "extractor": info.get("extractor_key") or "",
    }


def _find_media(work_dir: Path) -> Path:
    files = [
        item
        for item in work_dir.iterdir()
        if item.is_file() and not item.name.endswith(_MEDIA_IGNORE)
    ]
    if not files:
        raise DownloadFailed("media not found")
    media = max(files, key=lambda item: item.stat().st_size)
    if media.suffix.lower() not in VIDEO_SUFFIXES:
        raise DownloadFailed("not a video")
    return media


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _classify(stderr: str) -> LinkError:
    text = (stderr or "").lower()
    if "max-filesize" in text or "larger than" in text or "file is larger" in text:
        return TooLarge("too large")
    if "does not pass filter" in text:
        return TooLong("too long")
    if any(word in text for word in ("private", "unavailable", "not available", "removed", "deleted")):
        return DownloadFailed("unavailable")
    if any(word in text for word in ("login", "sign in", "cookies", "rate-limit", "http error 429")):
        return DownloadFailed("restricted")
    return DownloadFailed("downloader error")


async def _attempt(url: str, work_dir: Path, fmt: str, timeout: float):
    args = [
        YTDLP_BIN,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "--restrict-filenames",
        "--no-exec",
        "--no-write-playlist-metafiles",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_FILE_BYTES),
        "--write-info-json",
        "-f", fmt,
        "-o", str(work_dir / "video.%(ext)s"),
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        raise DownloadTimeout("timeout")

    if proc.returncode != 0:
        raise _classify(stderr.decode("utf-8", errors="ignore"))

    info = {}
    info_path = work_dir / "video.info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}

    media = _find_media(work_dir)
    size = media.stat().st_size
    if size > MAX_FILE_BYTES:
        raise TooLarge("too large after merge")

    duration = info.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration and duration > MAX_DURATION_SECONDS:
        raise TooLong("too long")

    return media, info, size, duration


async def download(normalized_url: str) -> DownloadResult:
    if normalized_url in _active_urls:
        raise LinkBusy("already downloading")
    _active_urls.add(normalized_url)
    work_dir = None
    try:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(TMP_ROOT).free
        if free < MAX_FILE_BYTES * 3:
            raise DownloadFailed("low disk space")

        async with _semaphore():
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT
            for fmt in FORMATS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DownloadTimeout("timeout")
                work_dir = Path(tempfile.mkdtemp(prefix="dl-", dir=TMP_ROOT))
                try:
                    media, info, size, duration = await _attempt(
                        normalized_url, work_dir, fmt, remaining
                    )
                except TooLarge:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    work_dir = None
                    continue
                except TooLong:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    work_dir = None
                    raise
                except Exception:
                    shutil.rmtree(work_dir, ignore_errors=True)
                    work_dir = None
                    raise

                result = DownloadResult(
                    path=media,
                    size=size,
                    duration=duration,
                    url=normalized_url,
                    meta=build_meta(info),
                )
                work_dir = None  # владение переходит вызывающему
                return result

            raise TooLarge("cannot fit into size limit")
    finally:
        _active_urls.discard(normalized_url)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
