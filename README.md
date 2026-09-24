# STAR TeamTalk Bot

Bridges [STAR](https://github.com/samtupy/star) coagulator TTS into a TeamTalk 5
channel. The bot connects to a TeamTalk server, joins a channel, and streams
synthesized speech (from a STAR coagulator over websockets) into that channel.

It only responds to **private messages** sent directly to it (PM-only mode).

## Requirements

- Windows, Linux, or another supported platform with the TeamTalk 5 SDK native
  library. The easiest way to get it: run the bundled downloader (below), which
  fetches the matching SDK for your OS/architecture into
  `_tt_vendor/TeamTalk_DLL/` — no TeamTalk install required. Alternatives: set
  `TT_SDK_DIR` to a directory containing the library, or install the TeamTalk
  client (Windows fallback) / SDK libs into a standard system path.
- `ffmpeg` on PATH (for transcoding synthesized audio to a streamable WAV).
- A running STAR coagulator (websocket) the bot can reach.
- [uv](https://github.com/astral-sh/uv) for the Python environment.

## Project layout

Everything lives in **one folder** (the cloned repo). No nested `src/` maze:

```
star_tt_bot/                  <- the clone (your "1 thingy")
├── run.py                    <- entry point (run this)
├── bot.py                    <- the bot logic
├── tt_sdk.py                 <- cross-platform SDK library locator/loader
├── config.py                 <- defaults + env-var overrides
├── star_client.py            <- STAR coagulator websocket client
├── tools/fetch_sdk.py        <- downloads + vendors the TeamTalk SDK
├── config.local.py.example   <- copy to config.local.py, fill in creds
├── _tt_vendor/TeamTalkPy/    <- vendored SDK wrapper (committed)
├── _tt_vendor/TeamTalk_DLL/  <- vendored native SDK lib (fetched, gitignored)
├── pyproject.toml / uv.lock
├── README.md / LICENSE / .gitignore
```

After `uv sync` you may also have a gitignored `config.local.py` here (your
real credentials — never committed).

## Setup

```bat
cd star_tt_bot
uv sync
uv run python tools/fetch_sdk.py   # one-time: download + vendor the TeamTalk SDK
uv run python run.py --set         # interactive: enter your account details
uv run python run.py               # run it
```

`--set` writes a gitignored `config.local.py` so your
credentials never leave your machine. Re-run with `--force` to overwrite.

To run without auto-connecting to a STAR coagulator:

```bat
uv run python run.py --no-star
```

## Commands (send as a private message to the bot)

- `/coag <ws://user:pass@host:port>` — connect to a STAR coagulator **for this
  session only** (does not change your saved default).
- `/coagulator <uri>` — alias of `/coag`.
- `/coag stop` — disconnect from the coagulator.
- `/voices` — list available voices, one per line.
- `/voice <name>` — select the active voice (e.g. `/voice sam`).
- `/rate <n>` — set speech rate (e.g. `/rate 200`). Injected into the spoken
  text as `[[rate n]]` (usually words/minute for most synths).
- `/pitch <n>` — set speech pitch (e.g. `/pitch 80`). Injected into the spoken
  text as `[[pbas n]]` (a voice-dependent number, per Sam Tupy's STAR spec).
- `/stop` — stop the current streaming speech.
- `/help` — show the command list.

Switching voices resets rate/pitch to defaults (values are per-voice).

**Any PM without a leading slash is spoken aloud**, announced as
`"<nickname> (<username>) said: <message>"`.

> **Rate/pitch mechanism.** Per Sam Tupy (STAR author), rate and pitch are
> inserted as literal tags anywhere in the speech string: `[[rate XXX]]` and
> `[[pbas XXX]]`, where XXX is usually words-per-minute for rate and a
> voice-dependent number for pitch. The bot injects these tags into the text
> before sending (only when you've set them). The server's provider consumes
> them. Some novelty voices speak the brackets literally — that's a voice
> quirk, not a bot bug. Experiment per voice to find good values.

## Configuration

**No secrets are stored in the repo.** Credentials come from environment
variables, a gitignored `config.local.py` (made via `--set`), or both. See
`config.py` for the full list of `STAR_TT_*` / `STAR_COAG_*`
environment variables.

Defaults (overridable via env or `config.local.py`):

| Setting        | Default                       |
|----------------|-------------------------------|
| host           | `tunmi13.com`                 |
| tcp/udp port   | `9483`                        |
| nickname       | `starbot`                     |
| username       | `star`                        |
| channel        | `/hangout area/`              |
| STAR coag URI  | `wss://star.blindsoft.net`    |

Set your real password with the `STAR_TT_PASSWORD` environment variable or via
`uv run python run.py --set`.

## TeamTalk SDK note

The Python `teamtalk` package normally tries to download a paywalled SDK from
bearware.dk on first import, and the SDK itself is only distributed as a 7z
behind an anti-bot check. `tools/fetch_sdk.py` (adapted from seedy60/cider)
solves that check, downloads the newest SDK for your platform (win64, win32,
ubuntu22_x86_64, raspbian_arm64), and installs `TeamTalk_DLL` + `TeamTalkPy`
into `_tt_vendor/`. The native library is resolved at runtime by `tt_sdk.py`:
vendored first, then `$TT_SDK_DIR`, then system locations — so the same repo
runs on Windows, Linux, or a Raspberry Pi without any TeamTalk client install.

If the automated download is blocked on your network, download the SDK 7z in a
browser and install it directly:

```bat
uv run python tools/fetch_sdk.py --archive path\to\tt5sdk_vX.YZ_win64.7z
```
