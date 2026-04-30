#!/usr/bin/env python3
"""lofi — minimalist terminal music player for YouTube livestreams."""

from __future__ import annotations

import argparse
import curses
import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


CONFIG_PATH = Path.home() / ".config" / "lofi" / "streams.toml"
VERSION = "0.1.0"


def _check_binary(name: str) -> str | None:
    if shutil.which(name) is None:
        return f"lofi: {name} not found. install with: brew install {name}"
    return None


@dataclass(frozen=True)
class Bookmark:
    name: str
    url: str
    title: str | None = None

    @property
    def display(self) -> str:
        return self.title or self.name


def load_config(path: Path) -> list[Bookmark]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    bookmarks: list[Bookmark] = []
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"{path}: [{name}] is not a table"
            )
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(
                f"{path}: [{name}] is missing 'url'"
            )
        title = entry.get("title")
        if title is not None and not isinstance(title, str):
            raise ValueError(
                f"{path}: [{name}] 'title' must be a string"
            )
        bookmarks.append(Bookmark(name=name, url=url, title=title))
    return bookmarks


DEFAULT_STREAMS_TOML = """\
# lofi config — add or remove streams below.
# each entry is a [name] table with a url and optional title.

[synthwave]
url = "https://www.youtube.com/watch?v=4xDzrJKXOOY"
title = "synthwave radio"

[lofi]
url = "https://www.youtube.com/watch?v=jfKfPfyJRdk"
title = "lofi hip hop radio"

[relax]
url = "https://www.youtube.com/watch?v=28KRPhVzCus"
title = "relax radio"
"""


def seed_config_if_missing(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_STREAMS_TOML)


# Resolver — wrap yt-dlp


class ResolverError(RuntimeError):
    pass


def resolve_stream_url(url: str, runner=subprocess.run) -> str:
    """Resolve a YouTube URL to its HLS manifest URL via yt-dlp.

    Returns the first line of stdout. Raises ResolverError on
    non-zero exit or empty output.
    """
    result = runner(
        ["yt-dlp", "-g", "--no-warnings", url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ResolverError(
            (result.stderr or "").strip()
            or f"yt-dlp exited {result.returncode}"
        )
    lines = (result.stdout or "").splitlines()
    line = lines[0].strip() if lines else ""
    if not line:
        raise ResolverError("yt-dlp returned no URL")
    return line


# MpvClient — JSON-IPC command sending


class MpvClient:
    """Sends JSON-IPC commands to a running mpv instance."""

    def __init__(self, sock) -> None:
        self._sock = sock

    def load(self, url: str) -> None:
        self._send({"command": ["loadfile", url, "replace"]})

    def pause(self) -> None:
        self._send({"command": ["set_property", "pause", True]})

    def resume(self) -> None:
        self._send({"command": ["set_property", "pause", False]})

    def stop(self) -> None:
        self._send({"command": ["stop"]})

    def _send(self, obj: dict) -> None:
        self._sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def start_mpv() -> tuple[subprocess.Popen, socket.socket, str]:
    """Launch a headless mpv and return (process, connected socket, socket_path).

    Caller is responsible for closing the socket and terminating the
    process.
    """
    socket_path = os.path.join(
        tempfile.gettempdir(), f"lofi-mpv-{os.getpid()}.sock"
    )
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    proc = subprocess.Popen(
        [
            "mpv",
            "--no-video",
            "--no-terminal",
            "--idle=yes",
            "--no-config",
            "--cache=yes",
            f"--input-ipc-server={socket_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 2.0
    while True:
        try:
            sock.connect(socket_path)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if proc.poll() is not None:
                sock.close()
                raise RuntimeError("failed to start mpv")
            if time.monotonic() > deadline:
                sock.close()
                proc.terminate()
                raise RuntimeError("failed to start mpv")
            time.sleep(0.05)
    return proc, sock, socket_path


def stop_mpv(
    proc: subprocess.Popen,
    sock: socket.socket,
    socket_path: str,
) -> None:
    try:
        sock.sendall(b'{"command":["quit"]}\n')
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass


@dataclass
class PlayerState:
    status: str = "idle"          # idle | resolving | playing | paused | error
    current: Bookmark | None = None
    error: str | None = None
    started_at: float = 0.0       # time.monotonic() when loadfile sent


# Synthwave color pairs (16-color fallback if 256 isn't available)
PAIR_PINK = 1
PAIR_CYAN = 2
PAIR_MAGENTA = 3
PAIR_GOLD = 4


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    if curses.COLORS >= 256:
        pink, cyan, mag, gold = 205, 51, 165, 227
    else:
        pink = curses.COLOR_MAGENTA
        cyan = curses.COLOR_CYAN
        mag = curses.COLOR_MAGENTA
        gold = curses.COLOR_YELLOW
    curses.init_pair(PAIR_PINK, pink, bg)
    curses.init_pair(PAIR_CYAN, cyan, bg)
    curses.init_pair(PAIR_MAGENTA, mag, bg)
    curses.init_pair(PAIR_GOLD, gold, bg)


def _attr(pair: int, *, bold: bool = False, dim: bool = False) -> int:
    a = curses.color_pair(pair)
    if bold:
        a |= curses.A_BOLD
    if dim:
        a |= curses.A_DIM
    return a


def _format_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _add_segments(stdscr, y: int, segments: list[tuple[str, int]]) -> None:
    try:
        stdscr.move(y, 0)
        for text, attr in segments:
            stdscr.addstr(text, attr)
    except curses.error:
        pass


def _draw_header(stdscr, w: int) -> None:
    pink = _attr(PAIR_PINK, bold=True)
    cyan = _attr(PAIR_CYAN, bold=True)
    title = [
        ("░▒▓█ ", pink),
        ("L O F I", cyan),
        (" █▓▒░", pink),
    ]
    used = sum(len(t) for t, _ in title)
    remaining = max(0, w - 1 - used)
    pattern = "▒▓█▓▒░"
    fill = (pattern * (remaining // len(pattern) + 1))[:remaining]
    _add_segments(stdscr, 0, title + [(fill, pink)])


def _draw_row(
    stdscr, y: int, b: Bookmark, is_cursor: bool, is_current: bool,
) -> None:
    cyan = _attr(PAIR_CYAN, bold=True)
    mag = _attr(PAIR_MAGENTA, bold=is_current)
    prefix_attr = cyan if is_cursor else mag
    _add_segments(stdscr, y, [
        ("> " if is_cursor else "  ", prefix_attr),
        (f"[{b.name}]", mag),
    ])


def _draw_horizon(stdscr, y: int, w: int) -> None:
    try:
        stdscr.addnstr(
            y, 0, "═" * max(0, w - 1), w - 1,
            _attr(PAIR_MAGENTA, bold=True),
        )
    except curses.error:
        pass


def _draw_status(stdscr, y: int, state: PlayerState) -> None:
    pink = _attr(PAIR_PINK, bold=True)
    cyan = _attr(PAIR_CYAN, bold=True)
    mag = _attr(PAIR_MAGENTA)
    mag_dim = _attr(PAIR_MAGENTA, dim=True)
    gold = _attr(PAIR_GOLD, bold=True)

    if state.status == "idle":
        _add_segments(stdscr, y, [("■ idle", mag_dim)])
    elif state.status == "resolving":
        name = state.current.display if state.current else ""
        _add_segments(stdscr, y, [
            ("… connecting to ", cyan),
            (name, cyan),
        ])
    elif state.status == "error":
        _add_segments(stdscr, y, [
            ("error: ", gold),
            (state.error or "unknown", mag_dim),
        ])
    elif state.status == "playing":
        elapsed = _format_elapsed(time.monotonic() - state.started_at)
        name = state.current.display if state.current else ""
        _add_segments(stdscr, y, [
            ("▶ ", pink),
            (f"{name}   ", cyan),
            (elapsed, mag),
        ])
    elif state.status == "paused":
        elapsed = _format_elapsed(time.monotonic() - state.started_at)
        name = state.current.display if state.current else ""
        _add_segments(stdscr, y, [
            ("‖ ", gold),
            (f"{name}   ", cyan),
            (elapsed, mag),
        ])


def _draw_keys(stdscr, y: int) -> None:
    pink = _attr(PAIR_PINK, bold=True)
    mag = _attr(PAIR_MAGENTA, dim=True)
    _add_segments(stdscr, y, [
        ("↑↓", pink), (" select   ", mag),
        ("⏎", pink), (" play   ", mag),
        ("space", pink), (" pause   ", mag),
        ("q", pink), (" quit", mag),
    ])


def _draw(stdscr, bookmarks: list[Bookmark], cursor: int, state: PlayerState) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    list_top = 1
    max_rows = max(0, h - 4)

    _draw_header(stdscr, w)

    if not bookmarks:
        try:
            stdscr.addnstr(
                list_top, 0,
                "no streams. edit ~/.config/lofi/streams.toml",
                w - 1, _attr(PAIR_MAGENTA, dim=True),
            )
        except curses.error:
            pass
    else:
        start = max(0, cursor - max_rows + 1) if cursor >= max_rows else 0
        visible = bookmarks[start : start + max_rows]
        for i, b in enumerate(visible):
            idx = start + i
            _draw_row(
                stdscr, list_top + i, b,
                is_cursor=(idx == cursor),
                is_current=(state.current is b),
            )
        hidden = len(bookmarks) - len(visible)
        if hidden > 0:
            try:
                stdscr.addnstr(
                    list_top + len(visible), 0,
                    f"  ... +{hidden} more", w - 1,
                    _attr(PAIR_MAGENTA, dim=True),
                )
            except curses.error:
                pass

    _draw_horizon(stdscr, h - 3, w)
    _draw_status(stdscr, h - 2, state)
    _draw_keys(stdscr, h - 1)
    stdscr.refresh()


def _resolve_in_background(bookmark: Bookmark, on_done) -> None:
    def worker() -> None:
        try:
            url = resolve_stream_url(bookmark.url)
            on_done(bookmark, url, None)
        except Exception as exc:  # ResolverError or OSError
            on_done(bookmark, None, str(exc))
    threading.Thread(target=worker, daemon=True).start()


def run_tui(
    stdscr,
    bookmarks: list[Bookmark],
    client: MpvClient,
) -> None:
    curses.curs_set(0)
    _init_colors()
    stdscr.timeout(500)  # redraw at least twice per second
    state = PlayerState()
    state_lock = threading.Lock()

    def set_error(msg: str) -> None:
        state.status = "error"
        state.error = msg

    def safe_ipc(action: str, fn) -> bool:
        try:
            fn()
            return True
        except OSError as exc:
            set_error(f"mpv {action} failed: {exc}")
            return False

    def on_resolved(bookmark, url, err) -> None:
        with state_lock:
            if state.current is not bookmark or state.status != "resolving":
                return
            if err:
                set_error(f"could not resolve {bookmark.name}")
                return
            if not safe_ipc("load", lambda: client.load(url)):
                return
            state.status = "playing"
            state.started_at = time.monotonic()

    # Auto-play synthwave on launch; fall back to first bookmark if absent.
    cursor = 0
    if bookmarks:
        auto = next(
            (b for b in bookmarks if b.name == "synthwave"), bookmarks[0]
        )
        cursor = bookmarks.index(auto)
        with state_lock:
            state.current = auto
            state.status = "resolving"
        _resolve_in_background(auto, on_resolved)

    while True:
        with state_lock:
            _draw(stdscr, bookmarks, cursor, state)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            break
        if ch == -1 or ch == curses.KEY_RESIZE:
            continue
        if ch in (ord("q"), 27):
            break
        if not bookmarks:
            continue
        if ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(bookmarks)
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(bookmarks)
        elif ch in (curses.KEY_ENTER, 10, 13):
            with state_lock:
                state.current = bookmarks[cursor]
                state.status = "resolving"
                state.error = None
            _resolve_in_background(bookmarks[cursor], on_resolved)
        elif ch == ord(" "):
            with state_lock:
                if state.status == "playing":
                    if safe_ipc("pause", client.pause):
                        state.status = "paused"
                elif state.status == "paused":
                    if safe_ipc("resume", client.resume):
                        state.status = "playing"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lofi",
        description="minimalist terminal music player for YouTube livestreams",
    )
    parser.add_argument(
        "--version", action="version", version=f"lofi {VERSION}"
    )
    parser.parse_args(argv)

    # Honour the user's locale so curses can render the Unicode glyphs.
    locale.setlocale(locale.LC_ALL, "")

    for binary in ("mpv", "yt-dlp"):
        msg = _check_binary(binary)
        if msg:
            print(msg, file=sys.stderr)
            return 1

    seed_config_if_missing(CONFIG_PATH)
    try:
        bookmarks = load_config(CONFIG_PATH)
    except Exception as exc:
        print(f"lofi: failed to read {CONFIG_PATH}: {exc}", file=sys.stderr)
        return 1

    try:
        proc, sock, sock_path = start_mpv()
    except RuntimeError as exc:
        print(f"lofi: {exc}", file=sys.stderr)
        return 1

    client = MpvClient(sock)
    try:
        curses.wrapper(run_tui, bookmarks, client)
    finally:
        stop_mpv(proc, sock, sock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
