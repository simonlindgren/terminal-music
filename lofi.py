#!/usr/bin/env python3
"""lofi — minimalist terminal music player for YouTube livestreams."""

from __future__ import annotations

import curses
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


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


KEY_HELP = "↑↓ select   ⏎ play   space pause   s stop   r reload   q quit"


def _format_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _glyph(status: str) -> str:
    return {"playing": "▶", "paused": "‖"}.get(status, "■")


def _draw(stdscr, bookmarks: list[Bookmark], cursor: int, state: PlayerState) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    max_rows = max(0, h - 4)

    if not bookmarks:
        stdscr.addnstr(
            0, 0,
            "no streams. edit ~/.config/lofi/streams.toml",
            w - 1,
        )
    else:
        visible = bookmarks[:max_rows]
        for i, b in enumerate(visible):
            prefix = "> " if i == cursor else "  "
            line = f"{prefix}{b.display:<28}  [{b.name}]"
            stdscr.addnstr(i, 0, line, w - 1)
        if len(bookmarks) > max_rows:
            stdscr.addnstr(
                max_rows, 0,
                f"  ... +{len(bookmarks) - max_rows} more", w - 1,
            )

    status_row = h - 2
    if state.status == "idle":
        status_line = f"{_glyph('idle')} idle"
    elif state.status == "resolving":
        name = state.current.display if state.current else ""
        status_line = f"… resolving {name}"
    elif state.status == "error":
        status_line = f"error: {state.error or 'unknown'}"
    else:  # playing or paused
        elapsed = _format_elapsed(time.monotonic() - state.started_at)
        name = state.current.display if state.current else ""
        status_line = f"{_glyph(state.status)} {name}   {elapsed}"

    stdscr.addnstr(status_row, 0, status_line, w - 1)
    stdscr.addnstr(h - 1, 0, KEY_HELP, w - 1)
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
    config_path: Path,
) -> None:
    curses.curs_set(0)
    stdscr.timeout(500)  # redraw at least twice per second
    cursor = 0
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
            # If user moved on, ignore stale results
            if state.current is not bookmark or state.status != "resolving":
                return
            if err:
                set_error(f"could not resolve {bookmark.name}")
                return
            if not safe_ipc("load", lambda: client.load(url)):
                return
            state.status = "playing"
            state.started_at = time.monotonic()

    while True:
        with state_lock:
            _draw(stdscr, bookmarks, cursor, state)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            break
        if ch == -1 or ch == curses.KEY_RESIZE:
            continue  # timeout or resize, redraw
        if ch in (ord("q"), 27):  # q or ESC
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
        elif ch == ord("s"):
            with state_lock:
                if state.status in ("playing", "paused"):
                    if safe_ipc("stop", client.stop):
                        state.status = "idle"
                        state.current = None
        elif ch == ord("r"):
            try:
                bookmarks[:] = load_config(config_path)
                cursor = min(cursor, max(0, len(bookmarks) - 1))
            except Exception as exc:
                with state_lock:
                    set_error(f"reload failed: {exc}")


def main() -> int:
    raise SystemExit("lofi: not yet implemented")


if __name__ == "__main__":
    raise SystemExit(main())
