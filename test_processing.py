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


class ProcessingTests(unittest.TestCase):
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
            self.assertAlmostEqual(result["duration"], 2, delta=0.09)
            self.assertAlmostEqual(result["original_duration"], 3, delta=0.01)

            # Sample the middle of both surviving intervals, and measure their zero-crossing rates.
            decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", result["audio_path"],
                                               "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
            samples = [item[0] for item in struct.iter_unpack("<h", decoded)]
            for offset, expected_hz in ((4000, rates[0]), (20000, rates[2])):
                sampleset = samples[offset:offset+8000]
                crossings = sum(a < 0 <= b for a, b in zip(sampleset, sampleset[1:]))
                self.assertAlmostEqual(crossings * 2, expected_hz, delta=6)


if __name__ == "__main__":
    unittest.main()
