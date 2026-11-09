import json
import re
from pathlib import Path


STUDIO_ROOT = Path(__file__).resolve().parents[1]


def load_profile(niche_id: str = "kids_3d") -> dict:
    """Load a niche profile and verify its local asset references."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", niche_id):
        raise ValueError("Invalid niche ID")

    profile_dir = (STUDIO_ROOT / "niches" / niche_id).resolve()
    profile = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or profile.get("id") != niche_id:
        raise ValueError(f"Profile ID must match {niche_id}")

    for field in ("name", "description", "style_guide", "sample_prompt"):
        if not _has_text(profile.get(field)):
            raise ValueError(f"Profile {niche_id} needs {field}")

    for field in ("audience_rules", "brand_rules"):
        rules = profile.get(field)
        if not isinstance(rules, list) or not rules or not all(map(_has_text, rules)):
            raise ValueError(f"Profile {niche_id} needs a list of {field}")

    examples = profile.get("few_shot")
    if not isinstance(examples, list) or not examples:
        raise ValueError(f"Profile {niche_id} needs at least one example")

    assets = profile.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError(f"Profile {niche_id} needs at least one asset")

    asset_ids = set()
    for asset in assets:
        if not isinstance(asset, dict) or not all(
            _has_text(asset.get(field))
            for field in ("id", "name", "description", "reference_image")
        ):
            raise ValueError(f"Profile {niche_id} has an incomplete asset")
        asset_id = asset["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", asset_id) or asset_id in asset_ids:
            raise ValueError(f"Invalid or duplicate asset ID {asset_id}")
        if asset.get("kind") not in ("character", "place", "item"):
            raise ValueError(f"Invalid asset kind for {asset_id}")

        relative_path = Path(asset["reference_image"])
        reference = (STUDIO_ROOT / relative_path).resolve()
        if (
            relative_path.is_absolute()
            or not reference.is_relative_to(profile_dir)
            or not reference.is_file()
        ):
            raise ValueError(f"Missing or invalid reference image for {asset_id}")
        asset_ids.add(asset_id)

    return profile


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
