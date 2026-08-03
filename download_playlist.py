#!/usr/bin/env python3
"""Download all tracks from a YouTube playlist as MP3 files."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import yt_dlp

SUPPORTED_BROWSERS = ("chrome", "edge", "firefox", "brave", "chromium", "opera", "vivaldi")


class _QuietLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


class CallbackLogger:
    """Routes yt-dlp log messages to a sink.

    Errors are always forwarded so failures are never silent. Debug/info/warning
    messages are only forwarded when ``debug`` is enabled.
    """

    def __init__(self, sink: Callable[[str], None], *, debug: bool = False) -> None:
        self._sink = sink
        self._debug = debug

    def _send(self, msg: object, prefix: str = "") -> None:
        text = msg.strip() if isinstance(msg, str) else str(msg)
        if text:
            self._sink(f"{prefix}{text}")

    def debug(self, msg: str) -> None:
        if not self._debug:
            return
        # yt-dlp routes both debug and info lines through debug();
        # info lines are not prefixed with "[debug]".
        self._send(msg)

    def info(self, msg: str) -> None:
        if self._debug:
            self._send(msg)

    def warning(self, msg: str) -> None:
        if self._debug:
            self._send(msg, prefix="warning: ")

    def error(self, msg: str) -> None:
        self._send(msg, prefix="yt-dlp error: ")


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def find_node() -> str | None:
    return shutil.which("node")


def build_js_runtimes() -> dict | None:
    node = find_node()
    if node:
        return {"node": {"path": node}}
    return None


def build_options(
    output_dir: Path,
    ffmpeg_location: str | None,
    keep_original: bool,
    browser: str | None,
    *,
    outtmpl: str | None = None,
    noplaylist: bool = False,
    logger: object | None = None,
    verbose: bool = False,
) -> dict:
    if outtmpl is None:
        outtmpl = str(
            output_dir / "%(playlist_title)s" / "%(playlist_index)03d - %(title)s.%(ext)s"
        )

    # Avoid the TVHTML5 client: YouTube often serves DRM-only formats there
    # ("This video is DRM protected") even for normal public videos.
    options: dict = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "ignoreerrors": True,
        "noplaylist": noplaylist,
        "keepvideo": keep_original,
        "quiet": not verbose,
        "no_warnings": not verbose,
        "noprogress": True,
        "verbose": verbose,
        "logger": logger if logger is not None else _QuietLogger(),
        "extractor_retries": 5,
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "-tv",
                    "web_safari",
                    "web_embedded",
                ],
            },
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "postprocessor_args": ["-ar", "44100"],
        "writethumbnail": False,
        "embedthumbnail": False,
    }

    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location

    js_runtimes = build_js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes

    if browser:
        options["cookiesfrombrowser"] = (browser.lower(),)

    # curl_cffi (optional extra) improves TLS fingerprinting for YouTube CDN requests.
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        options["impersonate"] = ImpersonateTarget.from_str("chrome")
    except ImportError:
        pass

    return options


def is_cookie_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("cookie", "dpapi", "decrypt", "keyring"))


def options_with_browser(options: dict, browser: str | None) -> dict:
    opts = dict(options)
    if browser:
        opts["cookiesfrombrowser"] = (browser.lower(),)
    else:
        opts.pop("cookiesfrombrowser", None)
    return opts


class BrowserCookieSession:
    """Use browser cookies when possible; fall back cleanly if decryption fails."""

    def __init__(
        self,
        browser: str | None,
        on_warn: Callable[[str], None] | None = None,
    ) -> None:
        self._browser = browser
        self._disabled = False
        self._warned = False
        self._on_warn = on_warn

    @property
    def browser(self) -> str | None:
        if self._disabled or not self._browser:
            return None
        return self._browser

    def _disable(self, exc: BaseException) -> None:
        if self._disabled or not self._browser:
            return
        self._disabled = True
        if self._on_warn and not self._warned:
            self._warned = True
            self._on_warn(
                f"Could not read {self._browser} cookies ({exc}). "
                "Continuing without browser cookies."
            )

    def extract_info(self, url: str, options: dict, *, download: bool = False):
        if self.browser:
            try:
                with yt_dlp.YoutubeDL(options_with_browser(options, self.browser)) as ydl:
                    return ydl.extract_info(url, download=download)
            except Exception as exc:
                if is_cookie_error(exc):
                    self._disable(exc)
                else:
                    raise
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=download)

    def download(self, url: str, options: dict) -> int:
        if self.browser:
            try:
                with yt_dlp.YoutubeDL(options_with_browser(options, self.browser)) as ydl:
                    return ydl.download([url])
            except Exception as exc:
                if is_cookie_error(exc):
                    self._disable(exc)
                else:
                    raise
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.download([url])


def resolve_browser(browser: str | None, no_browser: bool) -> str | None:
    if no_browser:
        return None
    return browser or None


def download_playlist(
    url: str,
    output_dir: Path,
    keep_original: bool = False,
    browser: str | None = None,
) -> int:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print(
            "Error: ffmpeg is required to convert audio to MP3.\n"
            "Install it with: winget install Gyan.FFmpeg",
            file=sys.stderr,
        )
        return 1

    if not build_js_runtimes():
        print(
            "Error: a JavaScript runtime is required for YouTube downloads.\n"
            "Install Node.js from https://nodejs.org/ or run: winget install OpenJS.NodeJS.LTS",
            file=sys.stderr,
        )
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = build_options(output_dir, ffmpeg, keep_original, browser)

    print(f"Saving MP3s to: {output_dir.resolve()}")
    print(f"Playlist URL: {url}")
    if browser:
        print(f"Using cookies from: {browser}")
    print()

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        errors = ydl.download([url])

    if errors:
        print(
            f"\nFinished with {errors} failed item(s).",
            file=sys.stderr,
        )
        if not browser:
            print(
                "Tip: if you still see HTTP 403 errors, retry with browser cookies, e.g.\n"
                "  python music_downloader.py \"PLAYLIST_URL\" --browser edge",
                file=sys.stderr,
            )
        return 1

    print("\nDone.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download all videos from a YouTube playlist as MP3 files."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="YouTube playlist URL (e.g. https://www.youtube.com/playlist?list=...)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Output folder (default: downloads)",
    )
    parser.add_argument(
        "-k",
        "--keep-original",
        action="store_true",
        help="Keep the downloaded audio file (e.g. .webm) after converting to MP3",
    )
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help=(
            "Load YouTube cookies from this browser to avoid HTTP 403 errors "
            "(optional; may fail on Windows if the browser is open or uses App-Bound Encryption)"
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not load cookies from a browser",
    )
    args = parser.parse_args()

    browser = resolve_browser(args.browser, args.no_browser)

    url = args.url
    if not url:
        url = input("Paste YouTube playlist URL: ").strip()

    if not url:
        print("Error: no playlist URL provided.", file=sys.stderr)
        return 1

    if "list=" not in url and "playlist" not in url.lower():
        print(
            "Warning: this does not look like a playlist URL. "
            "A single video will be downloaded instead.",
        )

    return download_playlist(url, args.output, args.keep_original, browser)


if __name__ == "__main__":
    raise SystemExit(main())
