"""Offline checks for timestamp boundaries, processing retries and the actual audio edit."""

import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import podcast_processing as pipeline


def decode_mono(path):
    pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path),
                                   "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
    return [sample for sample, in struct.iter_unpack("<h", pcm)]


def tone_amplitude(samples, hz):
    angle = math.tau * hz / 16000
    return 2 * math.hypot(sum(s * math.cos(i * angle) for i, s in enumerate(samples)),
                          sum(s * math.sin(i * angle) for i, s in enumerate(samples))) / len(samples)


class ProcessingTests(unittest.TestCase):
    def test_bloomberg_minute_crosses_model_blocks_and_transcript_lines(self):
        lines = ["Interview continues."] * 176 + [
            "This is the Bloomberg", "Market Minute, brought to you by our sponsor.",
            "Markets opened higher.", "The rally lasted into the afternoon.",
            "That was the Bloomberg Market", "Minute, brought to you by our sponsor.",
            "Back to the interview.", "We discussed the Bloomberg Market Minute yesterday.",
        ]
        rows = [{"start": i * 4, "end": i * 4 + 3, "text": line} for i, line in enumerate(lines)]
        # Even when the model returns no ads, the paired announcements enclose the entire report.
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "_ask", return_value={"ads": []}) as ask:
            ads, removed = pipeline._detect_ads(rows, "Example show", Path(folder), lambda stage: None)
        self.assertEqual([(ad["start"], ad["end"]) for ad in ads], [(704, 727)])
        self.assertEqual(removed, set(range(176, 182)))
        self.assertTrue(all(pipeline.BLOOMBERG_MINUTE_TASK in call.args[0] for call in ask.call_args_list))

    def test_minute_bookends_preserve_mixed_lines_and_require_a_nearby_matching_signoff(self):
        def cuts(lines, stride=4):
            rows = [{"start": i * stride, "end": i * stride + 3, "text": line} for i, line in enumerate(lines)]
            return [(ad["start"], ad["end"]) for ad in pipeline._bloomberg_minute_breaks(rows)]

        self.assertEqual(cuts(["A guest finishes. This is the Bloomberg Tech Minute.", "A news brief.",
                               "That's the Bloomberg Tech Minute. Back to our guest."]), [(4, 7)])
        self.assertEqual(cuts(["This is the Bloomberg Tech Minute.", "A news brief.",
                               "That's the Bloomberg Tech Minute brought to you by Acme. Get started at acme.com.",
                               "Back to our guest."]), [(0, 11)])
        self.assertEqual(cuts(["This is the Bloomberg Market Minute.", "A news brief.", "That was the Bloomberg",
                               "Market Minute,", "brought to you by Acme.", "Get started at acme.com.",
                               "Back to our guest."]), [(0, 23)])
        self.assertEqual(cuts(["This is the Bloomberg Minute.", "An unrelated discussion."]), [])
        self.assertEqual(cuts(["This is the Bloomberg Tech Minute.", "That was the Bloomberg Market Minute."]), [])
        self.assertEqual(cuts(["This is the Bloomberg Minute.", "That was the Bloomberg Minute."], stride=180), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg")
    def test_recovers_missed_sponsor_and_editorial_speech_without_filling_silence(self):
        first = {"start": 9, "end": 12, "text": "First."}
        second = {"start": 24, "end": 28, "text": "Second."}
        def word(start, end, text):
            return {"start": start, "end": end, "word": text}
        recoveries = [
            # First three seconds of each pass include already-transcribed speech.
            [{"start": 0, "end": 4, "text": "First.", "words": [word(2.8, 3.8, "First.")]},
             {"start": 5, "end": 17, "text": "Sponsor offer. Second.",
              "words": [word(5, 8, " Sponsor"), word(8, 10, " offer."), word(16, 17, " Second.")]},
             {"start": 10, "end": 12, "text": "Noise.", "no_speech_prob": .93, "avg_logprob": -1.2,
              "words": [word(10, 12, "Noise.")]}],
            [{"start": 6, "end": 9, "text": "Closing.", "words": [word(6, 9, " Closing.")]}],
        ]
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            source = cache / "speech.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", ""))
                audio.writeframes(b"\x00\x00" * (16000 * 9))
                audio.writeframes(struct.pack("<h", 500) * (16000 * 31))
            with patch.object(pipeline, "_transcriber", return_value=("mlx", "python", "model")), \
                 patch.object(pipeline, "_transcribe", side_effect=[[first, second], *recoveries]) as tr:
                actual = pipeline._segments(source, 40, cache, lambda stage: None)
                self.assertEqual(actual, [first, {"start": 14, "end": 19, "text": "Sponsor offer."},
                                          second, {"start": 31, "end": 34, "text": "Closing."}])
                self.assertEqual(actual, pipeline._segments(source, 40, cache, lambda stage: None))
                self.assertEqual(tr.call_count, 3)

    def test_rejects_unanchored_model_ranges(self):
        segments = [{"start": 0.0, "end": 2.0, "text": "Interview."}]
        with tempfile.TemporaryDirectory() as folder, patch.object(
            pipeline, "_ask", return_value={"ads": [{"first": 0, "last": 12, "reason": "Sponsor."}]}
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid transcript boundaries"):
                pipeline._detect_ads(segments, "Test", Path(folder), lambda stage: None)

    def test_preserves_all_ad_recording(self):
        segments = [{"start": 0.0, "end": 2.0, "text": "Our sponsor."}]
        with tempfile.TemporaryDirectory() as folder, patch.object(
            pipeline, "_ask", side_effect=[{"ads": [{"first": 0, "last": 0, "reason": "Sponsor."}]}, {"ads": []}]
        ):
            with self.assertRaisesRegex(RuntimeError, "Every spoken segment"):
                pipeline._detect_ads(segments, "Test", Path(folder), lambda stage: None)
            ads, _ = pipeline._detect_ads(segments, "Test", Path(folder), lambda stage: None)
            self.assertEqual(ads, [])

    def test_close_cuts_chain_and_remove_intervening_speech_from_notes(self):
        spans = [(0, 2), (2, 4), (4, 8), (8, 10), (10, 15), (15, 17), (17, 22.01), (22.01, 24)]
        segments = [{"start": a, "end": b, "text": f"Line {i}"} for i, (a, b) in enumerate(spans)]
        decision = {"ads": [{"first": i, "last": i, "reason": "Sponsor."} for i in (1, 3, 5, 7)]}
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "_ask", return_value=decision):
            ads, removed = pipeline._detect_ads(segments, "Test", Path(folder), lambda stage: None)
        self.assertEqual([(ad["start"], ad["end"]) for ad in ads], [(2, 17), (22.01, 24)])
        self.assertEqual(removed, {1, 2, 3, 4, 5, 7})

    def test_long_multilingual_episode_includes_last_section(self):
        sources = []

        def notes(task, source, schema):
            sources.append(source["material"])
            return {"text": "Opening notes" if len(sources) == 1 else "Later notes"}

        lines = ["城市雨量記錄" * 50 for _ in range(400)] + ["Final interview"]
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, "_ask", side_effect=notes):
            material = pipeline._summary_source(lines, "Rain", Path(folder), lambda stage: None)
        self.assertIn("Opening notes", material)
        self.assertIn("Final interview", sources[-1])
        self.assertTrue(all(len(part.encode()) <= pipeline.MAX_TEXT_BYTES for part in sources))

    def test_tool_timeout_terminates_descendants_holding_output_pipes(self):
        script = ("import subprocess,time; "
                  "subprocess.Popen(['sleep','10']); time.sleep(10)")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            pipeline._run([sys.executable, "-c", script], "Test child", timeout=0.35)
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_episode_retains_editorial_audio_and_reuses_completed_steps(self):
        rates = [240, 840, 1200]
        rows = [{"start": i, "end": i + 1, "text": text} for i, text in enumerate(
            ["Record rainfall each morning.", "Use sponsor code RAIN.", "Keep your notes for comparison."])]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "fixture.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", ""))
                for hz in rates:
                    audio.writeframes(b"".join(struct.pack("<h", int(7000 * math.sin(i*hz*math.tau/16000)))
                                               for i in range(16000)))

            calls = []

            def ask(task, content, schema):
                calls.append(task)
                if schema == pipeline.AD_SCHEMA:
                    return {"ads": [{"first": 1, "last": 1, "reason": "Sponsor code and offer."}]}
                self.assertNotIn("sponsor", content["material"].lower())
                return {"text": "Daily records make comparisons easier."}

            def download(url, path, *, max_bytes):
                shutil.copyfile(source, path)

            with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "PODCAST_LLM_BACKEND": "openai"}, clear=True), \
                 patch.dict(os.environ, {"PATH": os.defpath + ":/opt/homebrew/bin:/usr/local/bin"}), \
                 patch("podcast_network.download_to", side_effect=download) as downloader, \
                 patch.object(pipeline, "_transcribe", return_value=rows) as speech, \
                 patch.object(pipeline, "_ask", side_effect=ask):
                stages = []
                result = pipeline.process_episode("https://example.org/episode", root / "episode", "Weather", stages.append)
                repeated = pipeline.process_episode("https://example.org/episode", root / "episode", "Weather", stages.append)

            self.assertEqual(downloader.call_count, 1)
            self.assertEqual(speech.call_count, 1)
            self.assertEqual(calls, [pipeline.AD_TASK, pipeline.TAKEAWAYS, pipeline.SUMMARY])
            self.assertEqual(result, repeated)
            self.assertEqual(result["ads"][0]["start"], 1)
            self.assertIn("sponsor code", result["transcript"])
            expected_duration = 2 + pipeline.CHIME_SECONDS - 2 * pipeline.CROSSFADE_SECONDS
            self.assertAlmostEqual(result["duration"], expected_duration, delta=0.06)
            self.assertAlmostEqual(result["original_duration"], 3, delta=0.01)

            samples = decode_mono(result["audio_path"])
            self.assertAlmostEqual(len(samples) / 16000, expected_duration, delta=0.001)
            for offset, expected_hz in ((4000, rates[0]), (len(samples) - 12000, rates[2])):
                sampleset = samples[offset:offset+8000]
                crossings = sum(a < 0 <= b for a, b in zip(sampleset, sampleset[1:]))
                self.assertAlmostEqual(crossings * 2, expected_hz, delta=6)
            cue = samples[16400:18800]
            chime = tone_amplitude(cue, 440)
            self.assertGreater(chime, 200)
            self.assertLess(chime, tone_amplitude(samples[4000:12000], rates[0]) / 2)
            self.assertLess(tone_amplitude(cue, rates[1]), chime / 10)
            self.assertLess(tone_amplitude(samples[21400:22200], 440), chime / 3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_chimes_at_both_ends_and_between_quiet_and_loud_passages(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source.wav", Path(folder) / "edited.mp3"
            with wave.open(str(source), "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", ""))
                for hz, level in ((240, 1200), (1000, 12000)):
                    audio.writeframes(b"".join(struct.pack("<h", round(level * math.sin(i * hz * math.tau / 16000)))
                                               for i in range(112000)))
            pipeline._render(source, output, [], source, 14)
            original = decode_mono(output)
            self.assertEqual(len(original), 224000)
            self.assertLess(tone_amplitude(original[:16000], 440), 5)
            ads = [{"start": a, "end": b} for a, b in [(0, .25), (.5, .75), (6.75, 7.25), (13.25, 14)]]
            pipeline._render(source, output, ads, source, 14)
            samples = decode_mono(output)

        # The opening cuts absorb the quarter-second gap. Both six-second passages survive.
        self.assertAlmostEqual(len(samples) / 16000, 13.34, delta=.001)
        self.assertGreater(tone_amplitude(samples[12000:18000], 240), 800)
        self.assertGreater(tone_amplitude(samples[114000:120000], 1000), 8000)
        quiet, middle, loud = (tone_amplitude(samples[i:i+1600], 440) for i in (800, 104000, 206560))
        self.assertGreater(quiet, 100)
        self.assertGreater(min(middle, loud), quiet * 2)
        self.assertLess(max(map(abs, samples[:40])), quiet / 8)
        self.assertLess(max(map(abs, samples[-40:])), quiet / 8)


if __name__ == "__main__":
    unittest.main()
