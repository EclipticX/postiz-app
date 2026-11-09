import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.render import RenderError, render_video, sample_frames


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe")
class RenderFixtureTests(unittest.TestCase):
    def test_render_burns_approved_subtitles_and_samples_each_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            first = self.clip(work / "blue source.mp4", "blue", "160x90", 4)
            second = self.clip(work / "green source.mp4", "green", "90x160", 5)
            voice = self.tone(work / "voice.wav", 3)
            scenes = [
                {"scene_id": "one", "duration": 4, "dialogue": "Please water the little plant."},
                {"scene_id": "two", "duration": 5, "dialogue": ""},
                *({"scene_id": f"scene_{index}", "duration": duration, "dialogue": ""}
                  for index, duration in enumerate((4, 4, 4, 4, 5), start=3)),
            ]
            clips = {scene["scene_id"]: first if scene["duration"] == 4 else second for scene in scenes}
            transcribed = []

            def transcribe(path):
                transcribed.append(path)
                return [("Wrong", 0.35, 0.8), ("words", 1.0, 2.75)]

            result = render_video(
                scenes, clips, {"one": voice}, work / "studio's video.mp4",
                transcribe=transcribe,
            )

            probe = self.probe(result)
            video, audio = probe["streams"]
            self.assertEqual((video["width"], video["height"], video["codec_name"]), (720, 1280, "h264"))
            self.assertEqual(audio["codec_name"], "aac")
            self.assertAlmostEqual(float(probe["format"]["duration"]), 30.0, places=1)
            self.assertEqual(transcribed, [voice.resolve()])
            srt = result.with_suffix(".srt").read_text(encoding="utf-8")
            self.assertIn("00:00:00,350 --> 00:00:02,750", srt)
            self.assertIn("Please water the little plant.", srt)
            self.assertNotIn("Wrong", srt)

            frames = sample_frames(result, work / "QA", scenes)
            self.assertEqual(len(frames), 7)
            self.assertTrue(all(frame.stat().st_size > 0 for frame in frames))
            self.assertGreater(self.count_white_pixels(frames[0]), 30)
            self.assertEqual(self.count_white_pixels(frames[1]), 0)

            silent_samples = self.ffmpeg(
                "-ss", "6", "-i", str(result), "-t", "0.15", "-map", "0:a", "-f", "s16le", "-"
            )
            self.assertTrue(silent_samples)
            self.assertFalse(any(silent_samples), "Provider audio leaked into the silent scene")

    def test_silent_scene_can_mix_local_music_without_a_transcriber(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clip = self.clip(work / "source.mp4", "green", "90x160", 4)
            music = self.tone(work / "music.wav", 1)
            result = render_video(
                [{"scene_id": "one", "duration": 4, "dialogue": ""}], {"one": clip}, {},
                work / "preview.mp4", music_path=music,
            )

            self.assertAlmostEqual(float(self.probe(result)["format"]["duration"]), 4.0, places=1)
            self.assertFalse(result.with_suffix(".srt").exists())
            late_samples = self.ffmpeg(
                "-ss", "3", "-i", str(result), "-t", "0.15", "-map", "0:a", "-f", "s16le", "-"
            )
            self.assertTrue(any(late_samples), "The local music did not loop through the full scene")

    def test_structured_dialogue_uses_ordered_audio_and_custom_subtitle_style(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clip = self.clip(work / "scene.mp4", "blue", "90x160", 4)
            first = self.tone(work / "first.wav", 1, 440)
            second = self.tone(work / "second.wav", 1.4, 660)
            music = self.tone(work / "music.wav", 0.8, 220)
            effect = self.tone(work / "opening.wav", 0.55, 1100)
            scene = {
                "scene_id": "one", "duration": 4,
                "dialogue_lines": [
                    {"speaker_id": "tavi", "text": "Hello friend!"},
                    {"speaker_id": "milo", "text": "Let's play!", "voice_style_id": "bright"},
                ],
            }
            timings = {
                first.resolve(): [("different", 0.15, 0.35), ("words", 0.4, 0.65)],
                second.resolve(): [("different", 0.1, 0.55), ("words", 0.6, 1.25)],
            }
            result = render_video(
                [scene], {"one": clip}, {"one": [first, second]}, work / "preview.mp4",
                music_path=music, sfx_path=effect, transcribe=timings.__getitem__,
                subtitle_style={
                    "font_name": "DejaVu Sans", "font_size": 14, "text_color": "#FF0000",
                    "outline_color": "#FFFFFF", "position": "top",
                },
            )

            srt = result.with_suffix(".srt").read_text(encoding="utf-8")
            self.assertIn("00:00:00,150 --> 00:00:00,650\nHello friend!", srt)
            self.assertIn("00:00:01,100 --> 00:00:02,250\nLet's play!", srt)
            self.assertGreater(self.red_pixels(result, 0), 30)
            self.assertEqual(self.red_pixels(result, 980), 0)

            early, middle, late = (self.audio_samples(result, time) for time in (0.25, 1.25, 3.25))
            self.assertGreater(self.tone_power(early, 440), 500)
            self.assertGreater(self.tone_power(early, 1100), 100)
            self.assertGreater(self.tone_power(middle, 660), 500)
            self.assertLess(self.tone_power(middle, 1100), 30)
            self.assertGreater(self.tone_power(late, 220), 100)
            self.assertLess(self.tone_power(late, 660), 30)
            self.assertLess(self.tone_power(late, 1100), 30)

    def clip(self, path, color, size, duration):
        self.ffmpeg(
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=24:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=900:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        )
        return path

    def tone(self, path, duration, frequency=440):
        self.ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}", str(path))
        return path

    def red_pixels(self, path, y):
        pixels = self.ffmpeg(
            "-ss", "0.4", "-i", str(path), "-frames:v", "1", "-vf", f"crop=720:300:0:{y},scale=360:150",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        )
        return sum(
            pixels[index] > 180 and pixels[index + 1] < 65 and pixels[index + 2] < 65
            for index in range(0, len(pixels), 3)
        )

    def audio_samples(self, path, at):
        raw = self.ffmpeg("-ss", str(at), "-i", str(path), "-t", "0.2", "-ac", "1", "-ar", "16000", "-f", "s16le", "-")
        return struct.unpack(f"<{len(raw) // 2}h", raw)

    @staticmethod
    def tone_power(samples, frequency):
        angle = math.tau * frequency / 16000
        real = sum(sample * math.cos(angle * index) for index, sample in enumerate(samples))
        imaginary = sum(sample * math.sin(angle * index) for index, sample in enumerate(samples))
        return math.hypot(real, imaginary) / len(samples)

    def count_white_pixels(self, path):
        pixels = self.ffmpeg(
            "-i", str(path), "-vf", "crop=720:300:0:980,scale=360:150", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"
        )
        return sum(min(pixels[index:index + 3]) > 200 for index in range(0, len(pixels), 3))

    @staticmethod
    def ffmpeg(*args):
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            check=True, capture_output=True, timeout=120,
        ).stdout

    @staticmethod
    def probe(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True, capture_output=True, timeout=120,
        )
        return json.loads(result.stdout)


class NarrationValidationTests(unittest.TestCase):
    def test_spoken_scene_requires_a_local_whisper_model(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"STUDIO_WHISPER_MODEL_PATH": ""}):
            work = Path(directory)
            scene = {"scene_id": "one", "duration": 4, "dialogue": "Hello there."}
            clip = work / "clip.mp4"
            voice = work / "voice.wav"
            clip.touch()
            voice.touch()

            with self.assertRaisesRegex(RenderError, "STUDIO_WHISPER_MODEL_PATH"):
                render_video([scene], {"one": clip}, {"one": voice}, work / "preview.mp4")

    def test_rejects_narration_that_exceeds_approved_scene_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            scene = {"scene_id": "one", "duration": 4, "dialogue": "Hello there."}
            clip = work / "clip.mp4"
            voice = work / "voice.wav"
            clip.touch()
            voice.touch()

            with self.assertRaisesRegex(RenderError, "exceeds its 4-second scene"):
                render_video(
                    [scene], {"one": clip}, {"one": voice}, work / "preview.mp4",
                    transcribe=lambda path: [("Hello", 0, 1), ("there", 3.5, 4.9)],
                )
            self.assertFalse((work / "preview.mp4").exists())

    def test_structured_audio_must_fit_in_the_approved_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            scene = {"scene_id": "one", "duration": 4, "dialogue_lines": [
                {"speaker_id": "tavi", "text": "Hello."}, {"speaker_id": "milo", "text": "Hi there."},
            ]}
            clip, first, second = (work / name for name in ("clip.mp4", "first.wav", "second.wav"))
            for path in (clip, first, second):
                path.touch()
            with patch("app.render._audio_duration", side_effect=(2.2, 2.2)):
                with self.assertRaisesRegex(RenderError, "exceeds its 4-second scene"):
                    render_video(
                        [scene], {"one": clip}, {"one": [first, second]}, work / "preview.mp4",
                        transcribe=lambda path: [("Hello", 0.1, 1.5)],
                    )

    def test_rejects_unsafe_subtitle_style(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "preview.mp4"
            scenes = [{"scene_id": "one", "duration": 4, "dialogue": ""}]
            for style in (
                {"font_name": "DejaVu Sans,FontSize=50"}, {"font_size": 31},
                {"text_color": "&H00FF0000"}, {"position": "top'"},
            ):
                with self.subTest(style=style), self.assertRaisesRegex(RenderError, "subtitle|Subtitle"):
                    render_video(scenes, {}, {}, output, subtitle_style=style)
