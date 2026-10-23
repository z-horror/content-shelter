import asyncio
import os
import time

import pytest

from app.services import link_download

SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
out = None
for index, arg in enumerate(args):
    if arg == "-o":
        out = args[index + 1]
if not out:
    sys.exit(2)

mode = os.environ.get("FAKE_MODE", "ok")
sleep_for = float(os.environ.get("FAKE_SLEEP", "0"))
size = int(os.environ.get("FAKE_SIZE", "1000"))
duration = int(os.environ.get("FAKE_DURATION", "60"))

if sleep_for:
    time.sleep(sleep_for)
if mode == "fail":
    sys.stderr.write("ERROR: Video unavailable\n")
    sys.exit(1)

media = Path(out.replace("%(ext)s", "jpg" if mode == "image" else "mp4"))
media.parent.mkdir(parents=True, exist_ok=True)
with media.open("wb") as handle:
    handle.truncate(size)

info = {
    "title": "Test video",
    "uploader": "Tester",
    "description": "Description",
    "tags": ["one", "two"],
    "webpage_url": "https://www.youtube.com/watch?v=abcDEF12345",
    "duration": duration,
}
Path(out.replace("%(ext)s", "info.json")).write_text(json.dumps(info), encoding="utf-8")
'''


def _fake_bin(tmp_path, monkeypatch, mode="ok", size=1000, duration=60, sleep=0):
    path = tmp_path / "fake-yt-dlp"
    path.write_text(SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setattr(link_download, "YTDLP_BIN", str(path))
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("FAKE_SIZE", str(size))
    monkeypatch.setenv("FAKE_DURATION", str(duration))
    monkeypatch.setenv("FAKE_SLEEP", str(sleep))


def _setup(tmp_path, monkeypatch, **kwargs):
    _fake_bin(tmp_path, monkeypatch, **kwargs)
    monkeypatch.setattr(link_download, "TMP_ROOT", tmp_path / "downloads")


def test_normalize_urls():
    assert link_download.normalize_url("https://youtu.be/abcDEF12345") == (
        "https://www.youtube.com/watch?v=abcDEF12345"
    )
    assert link_download.normalize_url(
        "https://www.youtube.com/watch?v=abcDEF12345&t=10s"
    ) == "https://www.youtube.com/watch?v=abcDEF12345"
    assert link_download.normalize_url(
        "https://www.youtube.com/shorts/abcDEF12345"
    ) == "https://www.youtube.com/watch?v=abcDEF12345"
    assert link_download.normalize_url(
        "https://www.instagram.com/reels/XYZ_123/?igsh=abc"
    ) == "https://www.instagram.com/reel/XYZ_123/"


def test_normalize_url_rejects_unsupported():
    with pytest.raises(link_download.UnsupportedSource):
        link_download.normalize_url("https://example.com/video")
    with pytest.raises(link_download.InvalidLink):
        link_download.normalize_url("ftp://youtube.com/watch?v=abcDEF12345")
    with pytest.raises(link_download.InvalidLink):
        link_download.normalize_url("https://youtube.com/playlist?list=abc")
    with pytest.raises(link_download.InvalidLink):
        link_download.normalize_url("https://user:pass@youtube.com/watch?v=abcDEF12345")
    with pytest.raises(link_download.InvalidLink):
        link_download.normalize_url("https://youtube.com:8080/watch?v=abcDEF12345")


def test_download_success_and_metadata(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    result = asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))
    try:
        assert result.path.exists()
        assert result.size == 1000
        assert result.duration == 60
        assert result.meta["title"] == "Test video"
        assert result.meta["author"] == "Tester"
        assert result.meta["tags"] == ["one", "two"]
    finally:
        link_download.cleanup(result)

    assert list((tmp_path / "downloads").iterdir()) == []


def test_download_too_large_cleans_temp(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, size=5000)
    monkeypatch.setattr(link_download, "MAX_FILE_BYTES", 1000)

    with pytest.raises(link_download.TooLarge):
        asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))

    assert list((tmp_path / "downloads").iterdir()) == []


def test_download_too_long_cleans_temp(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, duration=700)

    with pytest.raises(link_download.TooLong):
        asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))

    assert list((tmp_path / "downloads").iterdir()) == []


def test_download_failure_cleans_temp(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="fail")

    with pytest.raises(link_download.DownloadFailed):
        asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))

    assert list((tmp_path / "downloads").iterdir()) == []


def test_download_timeout_kills_process(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, sleep=5)
    monkeypatch.setattr(link_download, "DOWNLOAD_TIMEOUT", 0.5)

    started = time.monotonic()
    with pytest.raises(link_download.DownloadTimeout):
        asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))
    assert time.monotonic() - started < 3
    assert list((tmp_path / "downloads").iterdir()) == []


def test_download_rejects_non_video(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, mode="image")

    with pytest.raises(link_download.DownloadFailed):
        asyncio.run(link_download.download("https://www.youtube.com/watch?v=abcDEF12345"))


def test_same_url_is_not_downloaded_twice(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, sleep=1)
    monkeypatch.setattr(link_download, "DOWNLOAD_TIMEOUT", 10)

    url = "https://www.youtube.com/watch?v=abcDEF12345"

    async def scenario():
        first = asyncio.create_task(link_download.download(url))
        while url not in link_download._active_urls:
            await asyncio.sleep(0.01)
        with pytest.raises(link_download.LinkBusy):
            await link_download.download(url)
        result = await first
        link_download.cleanup(result)

    asyncio.run(scenario())
    assert url not in link_download._active_urls


def test_cleanup_stale_touches_only_own_dir(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    (root / "old").mkdir()
    (root / "old" / "file").write_text("x")
    (root / "leftover").write_text("x")
    monkeypatch.setattr(link_download, "TMP_ROOT", root)

    link_download.cleanup_stale()

    assert root.exists()
    assert list(root.iterdir()) == []
