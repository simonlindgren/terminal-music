# lofi — minimalist terminal music player

A small terminal app for streaming YouTube livestreams (lofi radio,
jazz radio, etc.) without leaving the shell. No browser, no downloads,
no video. The user keeps a list of favourite streams in a config file
and picks one from a single-screen TUI.

## Scope

- Plays YouTube livestreams as audio only.
- Reads bookmarks from a TOML config file.
- Single-screen list TUI with keyboard navigation, play, pause, stop.
- One stream plays at a time.

Out of scope:
- Volume control (use system volume).
- Adding/editing bookmarks from inside the TUI.
- Non-YouTube sources.
- History, queueing, scrobbling.
- Recording or saving audio.

## Architecture

Single Python 3 script named `lofi`, installed on `PATH`. Uses only
the standard library, including `curses` for the TUI and `tomllib` for
config parsing.

External command-line dependencies, both installed via Homebrew:
- `yt-dlp` — resolves a YouTube livestream URL to its HLS manifest URL.
- `mpv` — plays the HLS stream, controlled over a Unix-domain IPC
  socket.

Process model:
- `lofi` launches one `mpv` child process at startup in headless
  audio-only idle mode. Flags: `--no-video --no-terminal --idle=yes
  --no-config --cache=yes --input-ipc-server=<socket>`. `--no-config`
  keeps the user's personal mpv config from interfering;
  `--cache=yes` smooths over short network drops.
- The mpv process lives for the duration of the `lofi` session.
- `lofi` sends JSON-IPC commands (`loadfile`, `set pause`, `stop`) to
  mpv over the socket.
- On quit, `lofi` sends `quit` to mpv and removes the socket.

Stream resolution flow when the user presses Enter on a bookmark:
1. Show "resolving <name>..." in the status line.
2. Run `yt-dlp -g --no-warnings <url>` in a background thread.
3. On success, send `loadfile <hls_url> replace` to mpv.
4. Update status line to show what is now playing.

`yt-dlp` runs in a thread so the TUI stays responsive during the
~1-2s resolve step.

## Config

Path: `~/.config/lofi/streams.toml`.

Format:

```toml
[lofi]
url = "https://www.youtube.com/watch?v=jfKfPfyJRdk"
title = "lofi hip hop radio"   # optional

[jazz]
url = "https://www.youtube.com/watch?v=Dx5qFachd3A"
title = "jazz radio"
```

- Each `[name]` table is one bookmark. The name is the short
  identifier shown in the list.
- `url` is required.
- `title` is optional; if absent, the name is shown alone.
- Order in the file is the order in the list.

Behaviour:
- If the config file is missing at startup, `lofi` creates the
  directory and writes a stub file containing a comment explaining
  the format, then opens with an empty list and a hint:
  `no streams. edit ~/.config/lofi/streams.toml`.
- If the config is malformed (TOML parse error), `lofi` exits with
  the parse error and the offending line number.
- The `r` key reloads the config without restarting playback.

## TUI

Plain `curses`. No box-drawing, no colour by default. A few Unicode
glyphs for play-state icons (`▶ ‖ ■`) and arrow hints. Single screen,
no panels.

Layout:

```
  lofi hip hop radio          [lofi]
> jazz radio                  [jazz]
  death metal radio           [metal]

  ▶ lofi hip hop radio   00:12:34
  ↑↓ select   ⏎ play   space pause   s stop   q quit
```

- Top region: list of bookmarks, one per line. Title on the left,
  short name in brackets on the right. The currently selected row is
  prefixed with `> `; others with `  `.
- Blank line.
- Status line: a play/pause/stop glyph (`▶`, `‖`, `■`), the title or
  name of what is playing, and elapsed time since the most recent
  `loadfile`. Shows `idle` when nothing is loaded.
- Key-hint line at the bottom.

Resize: redraw on `KEY_RESIZE`. If the terminal is smaller than the
list, the list is truncated and a `... +N more` line is shown.

## Controls

| key            | action                                |
|----------------|---------------------------------------|
| `↑` / `k`      | move selection up                     |
| `↓` / `j`      | move selection down                   |
| `⏎` (enter)    | play selected stream (replaces current) |
| `space`        | pause / resume                        |
| `s`            | stop                                  |
| `r`            | reload config                         |
| `q`            | quit (stops playback, exits)          |

Pressing `space` or `s` with nothing loaded is a no-op.

## Error handling

- `mpv` or `yt-dlp` not on `PATH` at startup: exit before drawing
  the TUI with a message such as
  `lofi: mpv not found. install with: brew install mpv`.
- `yt-dlp` exits non-zero or returns no URL: status line shows
  `error: could not resolve <name>`. The list stays usable.
- mpv IPC socket fails to connect within 2s of launch: exit with
  `lofi: failed to start mpv`.
- Network drops mid-stream: mpv's own cache and reconnect handle
  short drops. If mpv reports the stream ended, the status line
  shows `stream ended` and goes back to idle.
- Malformed TOML: exit with the parse error and line number.

## Layout of the script

A single file is fine for ~200 lines, but the responsibilities should
be separable so each piece can be reasoned about on its own:

- `config` — load and validate `streams.toml`, return a list of
  bookmark records. No UI, no subprocess.
- `mpv_client` — manage the mpv child process and the IPC socket.
  Exposes `load(url)`, `pause()`, `resume()`, `stop()`, `quit()`,
  and a way to read the current play state. No UI, no config.
- `resolver` — given a YouTube URL, return an HLS URL by shelling
  out to `yt-dlp`. No UI, no mpv.
- `tui` — the curses screen, key handling, and status updates.
  Pulls bookmarks from `config`, calls `mpv_client` and `resolver`,
  knows nothing about TOML or subprocess flags.
- `main` — argparse (just `--help` and `--version`), startup checks
  (binaries on PATH, config exists), wire the four units together.

In a single file, these are functions or small classes in clearly
separated sections. If the file grows past ~300 lines, split into a
small package.

## Testing

- `config` — unit tests for the loader: missing file creates stub,
  malformed file raises with line number, well-formed file parses to
  the expected list, optional `title` defaults correctly.
- `resolver` — one unit test that mocks `subprocess.run` and asserts
  the right arguments are passed and the captured stdout becomes the
  return value.
- `mpv_client` — unit tests that stub the socket and assert the
  right JSON-IPC commands are sent for `load`, `pause`, `resume`,
  `stop`.
- `tui` — not unit-tested; verified by hand on the real terminal.
- One end-to-end smoke check, run by hand: launch `lofi`, pick a
  bookmark, confirm audio plays, pause, resume, stop, quit.

## Install

A short README explains:
1. `brew install yt-dlp mpv`
2. Drop the `lofi` script somewhere on `PATH`, e.g.
   `~/.local/bin/lofi`, and `chmod +x`.
3. Create `~/.config/lofi/streams.toml` (or let `lofi` create the
   stub on first run).

No packaging, no `pyproject.toml`, no virtualenv. It is a single
script that runs against the system Python 3.11+ (for `tomllib`).
