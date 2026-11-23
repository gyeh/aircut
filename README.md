# Aircut

Aircut turns podcast subscriptions into a private RSS feed. Each episode gets a cleaned MP3, a timestamped transcript, takeaways, and a summary. The feed includes episode artwork when available and uses the show cover as a fallback.

Aircut replaces each detected ad break with a soft, half-second chime that crossfades into the surrounding audio. It merges cuts **five seconds apart or closer**, removing the audio between them and playing one chime for the whole break. Transcript timestamps refer to the original recording.

## Start listening

1. From the project directory on your Mac, run `./aircut start`.
2. Open the dashboard and choose **Add a feed**. Paste a show's RSS or Atom URL and choose how many existing episodes to process. [Open Aircut](http://127.0.0.1:8765)
3. Play a ready episode in the dashboard, or open **iPhone access** for a Safari pairing link and a private feed URL. In Apple Podcasts, choose **Library → Follow a Show by URL** and paste the feed URL.

Keep both links private and the Mac awake while streaming. Aircut checks for new episodes every 30 minutes. For a stable iPhone address, connect both devices to the same tailnet, run `tailscale serve --bg 8766` on the Mac, set `PODCAST_PUBLIC_URL` to the printed HTTPS origin in `.env`, and restart Aircut.

## Set up a new Mac

The server needs Python 3.10+. The default setup transcribes audio locally and sends transcript text to `gpt-6.2-astra` through the Codex CLI for ad detection and notes. On Apple Silicon with Homebrew and a compatible Codex CLI installed, run these commands from the project directory.

```sh
brew install ffmpeg uv cloudflared
uv venv --python 3.13 .runtime/mlx
uv pip install --python .runtime/mlx/bin/python mlx-whisper==0.4.3
.runtime/mlx/bin/hf download mlx-community/whisper-small-mlx
codex login
```

Use `./aircut status` to check the server and `./aircut stop` to stop it. Back up `.aircut/` to preserve your library, audio, and private links. Run the tests with `python3 -m unittest -v`.
