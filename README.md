# lofi

minimalist terminal music player for YouTube livestreams.

## install

```
brew install yt-dlp mpv
cp lofi.py ~/.local/bin/lofi
chmod +x ~/.local/bin/lofi
```

## use

run `lofi`. on first launch it seeds `~/.config/lofi/streams.toml`
with three streams (synthwave, lofi, relax) and starts playing
synthwave automatically. edit the config to add or remove
bookmarks.

keys: `↑↓` or `j k` select, `⏎` play, `space` pause,
`r` reload config, `q` quit.
