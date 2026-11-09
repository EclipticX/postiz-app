import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from app import niche


class NicheProfileTests(unittest.TestCase):
    def test_profile_and_reference_images_load_outside_studio(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                profile = niche.load_profile()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(profile["id"], "kids_3d")
        assets = {asset["id"]: asset for asset in profile["assets"]}
        self.assertEqual({asset["kind"] for asset in assets.values()}, {"character", "place", "item"})
        for asset in assets.values():
            reference = niche.STUDIO_ROOT / asset["reference_image"]
            self.assertEqual(ET.parse(reference).getroot().tag, "{http://www.w3.org/2000/svg}svg")

        scenes = profile["few_shot"][0]["scenes"]
        for scene in scenes:
            self.assertEqual(scene["duration"], 4)
            self.assertEqual(assets[scene["place"]]["kind"], "place")
            for character in scene["characters"]:
                self.assertEqual(assets[character]["kind"], "character")

    def test_rejects_an_asset_reference_outside_the_profile(self):
        profile = niche.load_profile()
        profile["assets"][0]["reference_image"] = "outside.svg"
        with tempfile.TemporaryDirectory() as directory:
            studio_root = Path(directory)
            profile_dir = studio_root / "niches" / "kids_3d"
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            (studio_root / "outside.svg").write_text("<svg/>", encoding="utf-8")
            with patch.object(niche, "STUDIO_ROOT", studio_root):
                with self.assertRaisesRegex(ValueError, "reference image"):
                    niche.load_profile()
