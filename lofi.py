#!/usr/bin/env python3
"""lofi — minimalist terminal music player for YouTube livestreams."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
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
                raise RuntimeError("failed to start mpv")
            if time.monotonic() > deadline:
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


def main() -> int:
    raise SystemExit("lofi: not yet implemented")


if __name__ == "__main__":
    raise SystemExit(main())
