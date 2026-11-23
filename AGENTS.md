# Repository Guidelines

## Project Structure

Aircut turns podcast subscriptions into a private RSS feed. Start with the module that owns the behavior you want to change.

- `aircut.py` serves the dashboard, HTTP API, audio, and RSS. The `aircut` shell script launches the server.
- `podcast_library.py` parses feeds, stores library and playback state in SQLite, and schedules episode processing.
- `podcast_processing.py` transcribes episodes, detects ad breaks, generates notes, and renders MP3s. Its transcript timestamps refer to the original recording.
- `podcast_network.py` fetches feeds and media. `podcast_access.py` manages private access and pairing.
- `static/` contains the dashboard HTML, CSS, JavaScript, and artwork. Playback controls live in `static/player.js`. Python tests live at the repository root.

## Local Development

Use Python 3.10 or newer. The server serves frontend assets directly. Follow `README.md` for the FFmpeg, MLX Whisper, and Codex CLI setup used to process episodes.

- `./aircut run` starts the foreground server. Open `http://127.0.0.1:8765` for the dashboard.
- `./aircut run --data "$(mktemp -d)" --port 8875 --gateway-port 8876` starts a separate instance with an empty library while the usual service is running.
- `./aircut start`, `./aircut status`, and `./aircut stop` manage the macOS background service.

## Coding Style

Use four-space indentation in Python, `snake_case` for functions and variables, and `PascalCase` for classes. Prefix module-private helpers with an underscore. In JavaScript, use two-space indentation and `camelCase` functions. Follow the surrounding code for formatting.

## Testing

Run `python3 -m unittest -v` from the root for the full suite, or `python3 -m unittest -v test_processing` for the audio pipeline. Install FFmpeg to exercise the audio-rendering tests. Place regression cases in the matching `test_*.py` module and name methods `test_<behavior>`. Use temporary libraries and mock external feeds and model responses.

## Commits and Pull Requests

Use a short, imperative commit subject. Recent subjects begin with verbs such as “Add” and “Merge.” In a pull request, describe the behavior change and how you tested it. Link a related issue when applicable and include a screenshot for visible dashboard changes. Redact private feed and pairing URLs from screenshots and logs.
