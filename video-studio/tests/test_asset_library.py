import io
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.asset_library import AssetLibrary, save_audio_upload
from app.db import StudioStore
from app.niche import load_profile


def png(red=80, width=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, 2, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((red, 170, 190)) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * 2))
        + chunk(b"IEND", b"")
    )


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8_000)
        audio.writeframes(b"\x00\x00" * 1_600)
    return buffer.getvalue()


REQUIRES_FFMPEG = unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe",
)


class AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "studio.sqlite3"
        self.store = StudioStore(self.path)
        self.store.initialize()
        self.store.seed_niche(load_profile())
        self.library = AssetLibrary(self.store)
        self.niche = "kids_3d"

    def asset(self, asset_id, library=None):
        return next(item for item in (library or self.library).list_assets() if item["id"] == asset_id)

    def test_seeded_references_keep_their_shape_and_path(self):
        asset = self.asset("tavi_seed_sprite")
        reference, = asset["references"]
        self.assertEqual(reference["id"], "bundled_1")
        self.assertEqual(reference["source"], "bundled")
        self.assertEqual(reference["angle"], "front")
        self.assertEqual(reference["variant_id"], "")
        self.assertTrue(reference["primary"])
        self.assertEqual(self.library.reference_path(reference).suffix, ".svg")

    def test_rejects_bad_asset_ids_and_duplicate_seeds(self):
        with self.assertRaises(ValueError):
            self.library.create_asset(self.niche, "../outside", "character", "Invalid", "Description")
        with self.assertRaises(ValueError):
            self.library.create_asset(self.niche, "tavi_seed_sprite", "character", "Replacement", "Description")
        self.assertEqual(self.asset("tavi_seed_sprite")["name"], "Tavi the Seed Sprite")

    def test_character_updates_keep_the_id_and_separate_appearance_colors(self):
        self.library.create_asset(self.niche, "mother", "character", "Mother", "Clay figure", "#be9020")
        self.assertEqual(self.library.get_character(self.niche, "mother")["dress_color"], "#BE9020")
        self.library.add_variant(self.niche, "mother", "rain_coat", "Rain coat", "Yellow rain coat.", "#eaad0c")
        self.library.update_character(self.niche, "mother", "Mother Bee", "Clay figure with round glasses", "#aabbcc")
        self.library.update_character(self.niche, "mother", "", "", "#324aB6", "rain_coat")
        with self.assertRaisesRegex(ValueError, "#RRGGBB"):
            self.library.update_character(self.niche, "mother", "Wrong name", "Description", "#faf")
        with self.assertRaises(LookupError):
            self.library.get_character(self.niche, "pebble_garden")

        character = self.library.get_character(self.niche, "mother")
        self.assertEqual(character["id"], "mother")
        self.assertEqual(character["name"], "Mother Bee")
        self.assertEqual(character["description"], "Clay figure with round glasses")
        self.assertEqual(character["dress_color"], "#AABBCC")
        self.assertEqual(character["variants"][0]["dress_color"], "#324AB6")

    @REQUIRES_FFMPEG
    def test_upload_separate_look_alike_characters_and_keep_one_primary_per_variant(self):
        for asset_id, name, detail in (
            ("mint_scout", "Mint Scout", "A round mint sprite with one orange leaf."),
            ("mint_friend", "Mint Friend", "A round mint sprite with two orange leaves."),
        ):
            self.library.create_asset(self.niche, asset_id, "character", name, detail)
        scout = self.library.add_reference(self.niche, "mint_scout", "front", "", False, png(40))
        friend = self.library.add_reference(self.niche, "mint_friend", "front", "", True, png(55))
        side = self.library.add_reference(self.niche, "mint_scout", "side", "", True, png(45))
        self.library.add_variant(self.niche, "mint_scout", "rain_coat", "Rain Coat", "Yellow rain coat.")
        variant = self.library.add_reference(
            self.niche, "mint_scout", "three-quarter", "rain_coat", False, png(50),
        )

        reopened_store = StudioStore(self.path)
        reopened_store.initialize()
        reopened = AssetLibrary(reopened_store)
        scout_asset = self.asset("mint_scout", reopened)
        friend_asset = self.asset("mint_friend", reopened)
        references = {ref["id"]: ref for ref in scout_asset["references"]}
        self.assertFalse(references[scout]["primary"])
        self.assertTrue(references[side]["primary"])
        self.assertEqual(references[side]["angle"], "side")
        self.assertTrue(references[variant]["primary"])
        self.assertEqual(references[variant]["variant_id"], "rain_coat")
        self.assertEqual(friend_asset["references"][0]["id"], friend)
        self.assertNotEqual(friend, scout)
        self.assertEqual(reopened.reference_path(references[variant]).read_bytes(), png(50))
        self.assertTrue(references[side]["path"].startswith("kids_3d/mint_scout/ref_"))
        self.assertTrue(friend_asset["references"][0]["path"].startswith("kids_3d/mint_friend/ref_"))

    @REQUIRES_FFMPEG
    def test_simultaneous_jpg_and_png_uploads_preserve_existing_references(self):
        jpg = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                "-i", "color=red:s=16x16:d=0.1", "-frames:v", "1", "-c:v", "mjpeg",
                "-f", "image2pipe", "pipe:1",
            ],
            capture_output=True, check=True, timeout=10,
        ).stdout
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.library.add_reference, self.niche, "tavi_seed_sprite", "side", "", True, jpg)
            second = pool.submit(self.library.add_reference, self.niche, "tavi_seed_sprite", "back", "", True, png())
            ids = {first.result(), second.result()}
        refs = self.asset("tavi_seed_sprite")["references"]
        self.assertEqual({ref["id"] for ref in refs}, {"bundled_1"} | ids)
        self.assertEqual(sum(ref["primary"] for ref in refs), 1)
        self.assertEqual(
            {self.library.reference_path(ref).suffix for ref in refs},
            {".svg", ".png", ".jpg"},
        )

    @REQUIRES_FFMPEG
    def test_retrying_a_reference_upload_id_does_not_add_or_replace_an_image(self):
        asset_id = "tavi_seed_sprite"
        upload_id = "ref_" + "a" * 32
        for angle, image in (("front", png()), ("side", png(50))):
            self.assertEqual(
                self.library.add_reference(self.niche, asset_id, angle, "", False, image, upload_id=upload_id),
                upload_id,
            )
        uploaded, = [ref for ref in self.asset(asset_id)["references"] if ref["source"] == "upload"]
        self.assertEqual(uploaded["angle"], "front")
        self.assertEqual(self.library.reference_path(uploaded).read_bytes(), png())

    @REQUIRES_FFMPEG
    def test_move_primary_reference_preserves_file_and_selects_replacement(self):
        asset_id = "tavi_seed_sprite"
        self.library.add_variant(self.niche, asset_id, "rain_coat", "Rain Coat", "Yellow rain coat.")
        reference_id = self.library.add_reference(self.niche, asset_id, "side", "", True, png())
        existing = self.library.add_reference(self.niche, asset_id, "front", "rain_coat", True, png(55))
        before = next(ref for ref in self.asset(asset_id)["references"] if ref["id"] == reference_id)
        self.library.update_reference(self.niche, asset_id, reference_id, "full-body", "rain_coat", True)
        after = {ref["id"]: ref for ref in self.asset(asset_id)["references"]}
        self.assertTrue(after["bundled_1"]["primary"])
        self.assertFalse(after[existing]["primary"])
        self.assertTrue(after[reference_id]["primary"])
        self.assertEqual(after[reference_id]["angle"], "full-body")
        self.assertEqual(after[reference_id]["variant_id"], "rain_coat")
        self.assertEqual(self.library.reference_path(before), self.library.reference_path(after[reference_id]))
        self.assertEqual(self.library.reference_path(before).read_bytes(), png())

    @REQUIRES_FFMPEG
    def test_invalid_image_payloads_and_paths_are_rejected(self):
        for invalid in (b"<svg/>", png()[:24], png()[:8] + png()[-12:], png(width=4097)):
            with self.subTest(size=len(invalid)):
                with self.assertRaises(ValueError):
                    self.library.add_reference(self.niche, "tavi_seed_sprite", "side", "", False, invalid)
        with self.assertRaisesRegex(ValueError, "Add this variant"):
            self.library.add_reference(self.niche, "tavi_seed_sprite", "side", "missing", False, png())
        self.assertFalse(self.library.root.exists())
        outside = self.path.parent / "outside.jpg"
        outside.write_bytes(b"private")
        self.library.root.mkdir()
        (self.library.root / "escape.jpg").symlink_to(outside)
        for path in ("../outside.jpg", str(outside), "escape.jpg"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.library.reference_path({"source": "upload", "path": path})

    def test_voice_styles_validate_and_have_at_most_one_default(self):
        self.library.save_voice_style(self.niche, "tavi_seed_sprite", "calm", "Calm", "Rachel", 0.8, True)
        self.library.save_voice_style(
            self.niche, "tavi_seed_sprite", "excited", "Excited", "Aria", "1.1", True, expressiveness="0.6",
        )
        styles = {style["id"]: style for style in self.asset("tavi_seed_sprite")["voice_styles"]}
        self.assertFalse(styles["calm"]["is_default"])
        self.assertIsNone(styles["calm"]["expressiveness"])
        self.assertTrue(styles["excited"]["is_default"])
        self.assertEqual(styles["excited"]["speed"], 1.1)
        self.assertEqual(styles["excited"]["expressiveness"], 0.6)
        with self.assertRaisesRegex(ValueError, "Voice styles belong to characters"):
            self.library.save_voice_style(self.niche, "pebble_garden", "calm", "Calm", "Rachel", 0.8, True)
        for speed, expression in ((1.25, None), (0.8, 1.1), (float("nan"), 0.5)):
            with self.subTest(speed=speed, expression=expression):
                with self.assertRaises(ValueError):
                    self.library.save_voice_style(
                        self.niche, "tavi_seed_sprite", "invalid", "Invalid", "Rachel", speed, False, expression,
                    )

    @REQUIRES_FFMPEG
    def test_audio_upload_uses_server_generated_name_and_checks_decode(self):
        audio_root = self.path.parent / "preset_audio"
        basename = save_audio_upload(audio_root, wav())
        self.assertRegex(basename, r"^audio_[0-9a-f]{32}\.wav$")
        self.assertEqual((audio_root / basename).read_bytes(), wav())
        with self.assertRaises(ValueError):
            save_audio_upload(audio_root, b"ID3bad media")
        self.assertEqual(list(audio_root.iterdir()), [audio_root / basename])

    @REQUIRES_FFMPEG
    def test_character_audio_replacement_cleans_old_upload_and_preserves_invalid_attempt(self):
        asset_id = "tavi_seed_sprite"
        self.library.save_character_audio(self.niche, asset_id, audio_bytes=wav())
        path = self.library.character_audio_path(self.niche, asset_id)
        self.assertEqual(path.read_bytes(), wav())
        with self.assertRaises(ValueError):
            self.library.save_character_audio(self.niche, asset_id, audio_bytes=b"bad audio")
        self.assertEqual(self.library.character_audio_path(self.niche, asset_id), path)

        url = "https://cdn.example.com/mother.wav?clip=sample"
        self.library.save_character_audio(self.niche, asset_id, url=url)
        self.assertEqual(self.asset(asset_id)["audio"], {"source": "link", "url": url})
        self.assertFalse(path.exists())
        with self.assertRaises(LookupError):
            self.library.character_audio_path(self.niche, asset_id)
        self.library.save_character_audio(self.niche, asset_id)
        self.assertIsNone(self.asset(asset_id)["audio"])

    def test_character_audio_links_require_https_without_credentials_or_controls(self):
        for invalid in (
            "http://example.com/sample.wav", "https://", "https://user@example.com/a.wav",
            "https://example.com/a\n.wav", "https://example.com\\a.wav", "https://example.com:invalid/a.wav",
        ):
            with self.subTest(url=invalid), self.assertRaisesRegex(ValueError, "HTTPS audio link"):
                self.library.save_character_audio(self.niche, "tavi_seed_sprite", url=invalid)
        self.assertIsNone(self.asset("tavi_seed_sprite")["audio"])


if __name__ == "__main__":
    unittest.main()
