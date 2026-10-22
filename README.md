# lossy-mirror

Mirrors a music library, converting lossless audio to a compact format on the way. For a music server or a phone.

- **Mirror:** everything the filter allows is copied; deleted or excluded source files disappear from the mirror; changed source files are redone.
- **Conversion:** mapped formats (e.g. flac, wav, ape) become one output format (e.g. ogg vorbis). The converter only produces audio. Tags are copied by the app, verbatim, multi-value tags included.
- **One target per track:** same-name audio files in a folder (`song.flac`, `song.mp3`) compete, the best wins (lossless first, then sample rate × bit depth or bitrate). The others are ignored with a `COLLISION` warning.
- **Never redoes work:** a target is up to date when its mtime equals the source's (and size, for copies).

Runs as a container: watches the source with inotify, starts a pass once activity settles, and runs a full sweep periodically to catch anything inotify missed.

## Behaviour

| Situation | Result |
|---|---|
| new or changed source file | converted or copied, target gets the source mtime |
| target mtime = source mtime | skipped |
| source deleted or no longer included | target deleted at the end of the pass |
| track's target changes format (e.g. mp3 replaced by flac, or output switched from ogg to opus) | old target deleted right after the new one lands, so servers like Navidrome see a move and keep play history |
| source tags cannot be read with certainty | file refused with an error, nothing written |

Writes are atomic: conversions are built in a local temp file, copied next to the target as `.tmp`, then renamed. More than 10% of the mirror to delete at once → deletion is skipped and reported.

## Audio engine

Tags are read from the source and written to the output by the app, never by the converter:

| Source | Tags |
|---|---|
| flac, ogg, opus | vorbis comments, 1:1 |
| ape, wv | APEv2, translated (`Album Artist` → `ALBUMARTIST`, `Track 3/12` → `TRACKNUMBER` + `TRACKTOTAL`, ...) |
| wav | RIFF INFO, translated (`INAM` → `TITLE`, ...); wav with ID3 is refused |
| anything else | refused as conversion input |

Outputs: ogg, opus or flac. Embedded pictures are not carried over: keep a `folder.jpg` next to the tracks.

## Configuration

`/config/mirror.toml`, written with defaults on first start, reloaded at every pass.

```toml
[filter]
rules = [
    "- .*/",          # hidden folders
    "+ *.flac",
    "+ *.mp3",
    "+ *.jpg",
    "+ */",           # keep folders traversable
    "- *",            # everything else
]
# file = "/src/.mirrorfilter"   # more rules from a file, applied after these

[[convert]]
inputs = ["flac", "wav", "ape"]   # order breaks ties between equal-quality sources
output = "ogg"
max_sample_rate = 48000            # {resample} becomes "-ar <rate>" above this
command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", "{input}",
           "-map", "0:a:0", "-map_metadata", "-1", "-threads", "1",
           "-c:a", "libvorbis", "-q:a", "4.1", "{resample}", "-f", "ogg", "{output}"]
```

Filter rules: rsync-style, matched against file and folder names (not paths). First match wins, no match = included. A trailing `/` limits the rule to folders.

Changing the conversion command does not redo existing targets (their mtime still matches). To force it, delete the affected part of the mirror.

## Environment

| Variable | Default | |
|---|---|---|
| `ARMED` | `0` | `0` = plan only: one dry-run pass logged as `*_PLAN.log`, nothing written. `1` = mirror |
| `JOBS` | `2` | files processed in parallel |
| `DEBOUNCE_SECONDS` | `60` | quiet time before a pass |
| `SWEEP_HOURS` | `12` | full pass interval |
| `MAX_DELETE_RATIO` | `0.1` | skip deletions above this share of the mirror |
| `WATCH_PATHS` | `/src` | `:`-separated paths to watch |
| `MODE` | `watch` | `once` = single pass, exit status = result |
| `PUID` / `PGID` | `99` / `100` | owner of written files |
| `UMASK` | `000` | |
| `LOG_COUNT` | `20` | log files kept |

Mounts: `/src` (read-only), `/dst`, `/config`.

Logs: stdout, plus `/config/logs/<timestamp>_OK.log` / `_ERRORED.log` / `_PLAN.log` for passes that changed something, failed, or were dry runs.

## Unraid

1. Copy `unraid/lossy-mirror.xml` to `/boot/config/plugins/dockerMan/templates-user/`.
2. Docker → Add Container → `lossy-mirror`. Leave `ARMED=0`, start.
3. Edit `/mnt/user/appdata/lossy-mirror/mirror.toml`, restart, read the `_PLAN.log`: everything to convert, copy and **delete**.
4. Set `ARMED=1`.

## One-off run elsewhere

```
docker run --rm -e MODE=once -e ARMED=1 -e JOBS=8 \
  -v /path/to/library:/src:ro -v /path/to/mirror:/dst -v ./config:/config \
  ghcr.io/trecoool/lossy-mirror
```

Stop the always-on container first: two writers on the same mirror is not supported.

CLI with all options (`--only Artist/Album`, `--dry-run`, ...):

```
docker run --rm --entrypoint python3 -v ... ghcr.io/trecoool/lossy-mirror /app/mirror.py --help
```
