# Aircut

Aircut turns podcast subscriptions into a private combined RSS feed and a separate feed for each show. Play an episode while it processes: Aircut labels it `[Original with ads]` until the cleaned MP3 is ready, then updates the same RSS item. Processed episodes also get a timestamped transcript and an editorial briefing with the big idea, themes, verified quotations, references, and takeaways.

Aircut replaces each detected ad break with a soft, half-second chime that crossfades into the surrounding audio. It merges cuts **five seconds apart or closer**, removing the audio between them and playing one chime for the whole break. The **Removed ads** section shows cut ranges, durations, advertisers when identified, and the total time removed. Transcript and cut timestamps refer to the original recording.

Published episode titles start with 🍊. When Aircut verifies the Mac’s private Tailscale Serve route, each RSS description links to a private episode page with the briefing, transcript, and removed ads. Open the link while connected to the same tailnet with a user-owned device. Verification requires the macOS Tailscale system extension and a private HTTPS root proxy to Aircut’s loopback gateway. If verification fails, Aircut omits the link and denies access to the page.

## Start listening

1. From the project directory on your Mac, run `./aircut start`.
2. Open the dashboard and choose **Add a feed**. Paste a show's RSS or Atom URL and choose how many existing episodes to process. [Open Aircut](http://127.0.0.1:8765)
3. Play a ready episode in the dashboard, or open **RSS feeds** to copy the combined feed URL or an individual show's URL. Choose **Open feeds.txt** for a copyable list with one URL per line. [Open RSS feeds](http://127.0.0.1:8765/#rss)
4. To listen on another device, open **iPhone access** to enable remote access. You can pair Safari there or copy a feed URL from **RSS feeds** and paste it into **Library → Follow a Show by URL** in Apple Podcasts.

Keep the feed and pairing links private and the Mac awake while streaming. Aircut checks for new episodes every 30 minutes. For a stable iPhone address, connect both devices to the same tailnet, run `tailscale serve --bg 8766` on the Mac, set `PODCAST_PUBLIC_URL` to the printed HTTPS origin in `.env`, and restart Aircut.

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

## Automatic episode retention

Automatic deletion is off by default. Set `PODCAST_RETENTION_MONTHS=2` in the
project’s `.env` and restart Aircut to remove episodes whose original feed
publication date is more than two calendar months old (UTC). Use a whole
number from 1 to 120, or 0 to disable cleanup. For a foreground run, use
`--retention-months` to override the setting.

Aircut checks at startup and every 15 minutes. It keeps saved and undated
episodes, and rechecks in-progress episodes after they finish. Subscriptions
are unchanged. Expired audio and notes are permanently deleted from Aircut
and removed from its dashboard and private RSS feeds. Use **Save** before an
episode expires to keep it. Retention does not remove downloads from other
podcast apps, and increasing the window does not restore deleted episodes.
