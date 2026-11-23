# Aircut

Aircut turns podcast subscriptions into a private listening feed. Each new episode gets a timestamped transcript, concise takeaways, a comprehensive summary, and a playable MP3 with detected advertising removed.

## Start listening on this Mac

Open [the local dashboard](http://127.0.0.1:8765). Choose **Add a feed**, paste the show's RSS or Atom URL, and choose how many existing episodes to process. The default is the latest episode. **New episodes only** starts with the next release. Aircut checks subscribed feeds every 30 minutes while the Mac is awake. **Subscriptions → Check feeds** triggers an earlier check.

Open an episode to follow processing, play the cleaned audio, read either set of notes, or inspect the transcript and detected ad intervals. Transcript timestamps and ad boundaries refer to the original recording. Failed jobs have a retry button. Unsubscribing stops future processing and preserves completed episodes.

Choose **Play** or **Resume at** beside an episode in the Library, a subscription, or Saved to listen directly from the list. The skip buttons move back 15 seconds or forward 30 seconds by default. You can change each interval in playback settings. Aircut remembers your place in each episode, so you can pause on the Mac and resume on your paired iPhone. Finishing an episode resets its playback position for a replay.

Use the bookmark on an episode to add it to **Saved**. Bookmarks, playback positions, and skip settings are shared across browsers connected to this Aircut library. Apple Podcasts keeps its own listening history and saved list when you play the RSS feed in that app.

Use the trash button beside an episode or choose **Delete** on its detail page to remove it from Aircut. Confirming removes the episode from the library and private feed, deletes its local audio, transcript, and notes, and prevents Aircut from importing it again on a feed check. Copies already downloaded in Apple Podcasts stay on that device.

### Server controls

Run these commands in the project directory. The background server runs in your macOS login session, even after you close Terminal. Start it again after logging back in or rebooting.

```sh
./aircut start
./aircut status
./aircut stop
```

Use `./aircut run` to run in the foreground. For example, `./aircut run --poll-minutes 10` checks every ten minutes. `./aircut run --help` lists port and data-directory options. Only one server can own a library at a time. The background service writes its log to `.aircut/server.log`.

## Listen on iPhone

On the Mac, open **iPhone access → Enable iPhone access**. Aircut creates an outbound HTTPS tunnel to the authenticated gateway. Copy the link you need to your iPhone.

- **Safari pairing link** opens the mobile listening room, including playback, subscriptions, summaries, and transcripts. Opening the link pairs the browser for 60 days. Treat this link like a password.
- **Private podcast feed** goes into Apple Podcasts under **Library → Add/Follow a Show by URL**. The Library menu label varies with iOS. Each ready episode appears with a seekable MP3 and both sets of notes in its description. The feed includes the publisher’s episode artwork when available and falls back to the original show’s cover. Anyone with this feed link can listen. [Apple's instructions](https://podcasters.apple.com/support/828-test-your-podcast)

Keep the server running and the Mac awake to stream. The temporary address changes when the tunnel restarts. If your phone initially cannot resolve the new hostname, give DNS a little time and reopen the link. You can close the tunnel from the same dialog. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

### Keep the same feed URL

For an ongoing subscription across restarts, point a stable HTTPS hostname at the **authenticated gateway on `127.0.0.1:8766`**. Port 8765 is the Mac's local management interface.

One option is Tailscale Serve. Sign in to the same tailnet on the Mac and iPhone. On the Mac, run `tailscale serve --bg 8766` and use its printed HTTPS origin. Keep Tailscale connected on the phone when using this address. A named Cloudflare Tunnel pointed at port 8766 also works with a hostname you manage. [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)

Create a file named `.env` next to `aircut.py` and replace the example hostname with your actual HTTPS origin.

```dotenv
PODCAST_PUBLIC_URL=https://your-mac.your-tailnet.ts.net
```

Restart Aircut using `./aircut stop` followed by `./aircut start`. The iPhone dialog now generates links for that hostname. Subscribe once to the stable feed.

## Processing and storage

On this Mac, Aircut uses the project-local MLX Whisper runtime with the cached multilingual Whisper Small model. Audio transcription runs on-device. Transcript text goes to **GPT-6.2 Astra** using the signed-in Codex CLI. Astra classifies ads first, then receives separate requests for short takeaways and a comprehensive summary using the retained editorial material. Model calls use the configured account's allowance.

The ad detector identifies sponsor reads and explicit advertisements from speech. It keeps uncertain passages and segments that mix editorial content with advertising. Unspoken ads or subtle promotions can remain, and recognition errors can affect cut boundaries. FFmpeg writes a new MP3 and Aircut retains the original audio.

The hidden `.aircut/` directory holds the SQLite library, audio, transcript caches, and link-signing key. Back up this folder to keep your subscriptions, episodes, and private links. Disk usage grows with the library. Individual jobs run one at a time and reuse completed transcription and model steps after a restart or retry.

## Recreate the runtime

The server uses Python 3.10+ and the standard library. Episode processing also requires FFmpeg and FFprobe. For the default model backend, install a compatible Codex CLI, sign in with `codex login`, and check that the account can run `gpt-6.2-astra`.

On an Apple Silicon Mac, the following commands recreate the local transcription environment from the project directory. They require Homebrew and uv, and download the runtime and model if needed.

```sh
brew install ffmpeg cloudflared
uv venv --python 3.13 .runtime/mlx
uv pip install --python .runtime/mlx/bin/python mlx-whisper==0.4.3
.runtime/mlx/bin/hf download mlx-community/whisper-small-mlx
```

Aircut discovers that runtime and the model in the default Hugging Face cache automatically. This Mac also has a project-local, checksum-verified Cloudflare tunnel binary. Aircut accepts either a `cloudflared` executable on PATH or `.runtime/cloudflared`.

### Alternate configuration

Settings in the process environment take precedence over `.env`. The file accepts one `NAME=value` per line. Restart the server after changes.

| Setting | Purpose |
| --- | --- |
| `PODCAST_MLX_PYTHON`, `PODCAST_MLX_MODEL` | Set both to use a particular MLX Python executable and local model directory. |
| `WHISPER_CPP_MODEL`, `WHISPER_CPP_BIN` | Select whisper.cpp with a local model file. The executable defaults to `whisper-cli`. |
| `OPENAI_API_KEY` | Enables hosted Whisper transcription when no local transcriber is configured. Hosted transcription uploads the audio in chunks. |
| `PODCAST_LLM_BACKEND` | Defaults to `codex`. Set to `openai` to use the Responses API with an API key that has access to your chosen model. |
| `PODCAST_LLM_MODEL` | Defaults to `gpt-6.2-astra`. Unavailable models produce a visible error. |
| `PODCAST_PUBLIC_URL` | Stable HTTPS origin of your configured gateway, with no path. |

Run `python3 -m unittest -v` for the offline test suite. The HTTP tests use temporary loopback listeners.
