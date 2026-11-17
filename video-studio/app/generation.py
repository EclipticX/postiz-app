"""Build an immutable generation choice from Settings and selected assets."""

from app.settings import DEFAULT_PROVIDERS, provider_blockers


DIRECT_MODEL = "fal-seedance-25-flash"
DIRECT_CAPABILITIES = next(spec[5] for spec in DEFAULT_PROVIDERS if spec[0] == DIRECT_MODEL)


def generation_options(form, policy, library):
    def field(name, default=""):
        return form.get(name, [default])[0].strip()

    characters = list(dict.fromkeys(form.get("character", ())))
    elements = list(dict.fromkeys(form.get("element", ())))
    voice = field("voice_reference")
    narration = field("narration")
    scene_plan = policy["scene_plan"]
    separate_voice = policy["separate_voice"]
    if scene_plan:
        duration, aspect, resolution = 30, "9:16", "720p"
        elements, voice, narration = [], "", ""
    else:
        model = field("model", DIRECT_MODEL)
        if model != DIRECT_MODEL:
            raise ValueError("Choose a supported direct video model.")
        try:
            duration = int(field("duration", "30"))
        except ValueError:
            raise ValueError("Choose a whole video duration from 4 to 30 seconds.") from None
        aspect, resolution = field("aspect_ratio", "9:16"), field("resolution", "720p")
        if duration not in DIRECT_CAPABILITIES["durations"]:
            raise ValueError("Choose a whole video duration from 4 to 30 seconds.")
        if aspect not in DIRECT_CAPABILITIES["aspect_ratios"] or resolution not in DIRECT_CAPABILITIES["resolutions"]:
            raise ValueError("Choose a supported aspect ratio and resolution.")
        if len(characters) + len(elements) > 10:
            raise ValueError("Choose at most 10 character and element image references.")
        if separate_voice and (not narration or len(narration) > 4000):
            raise ValueError("Enter up to 4,000 characters of narration in New Video or Prompt Schedule.")
        if separate_voice:
            voice = ""
        else:
            narration = ""

    if characters or elements or voice:
        saved = {item["id"]: item for item in library.assets.list_assets("kids_3d")}
        if any(saved.get(asset_id, {}).get("kind") != "character" for asset_id in characters):
            raise ValueError("Choose saved characters from the character list.")
        if any(saved.get(asset_id, {}).get("kind") not in ("place", "item") for asset_id in elements):
            raise ValueError("Choose saved elements from the asset list.")
        if voice and (voice not in characters or not saved[voice].get("audio")):
            raise ValueError("Choose a selected character with a saved voice reference.")
        profile = library.profile("kids_3d", {"characters": characters})
        if any(asset not in {item["id"] for item in profile["assets"]} for asset in elements):
            raise ValueError("Add a usable image to each selected element before generating.")

    return {"characters": characters, "generation": {
        "scene_plan": scene_plan, "separate_voice": separate_voice, "model": DIRECT_MODEL,
        "duration": duration, "aspect_ratio": aspect, "resolution": resolution,
        "elements": elements, "voice_reference": voice, "narration": narration,
    }}


def generation_rates(settings):
    routes = [route for route in settings.list_providers() if not provider_blockers(route)]
    rates = {route["name"]: route["rate_micros"] for route in routes}
    animation = [route["rate_micros"] for route in routes
                 if route["type"] == "video" and route["capabilities"].get("image_to_video")]
    return {
        "direct_per_second": rates.get(DIRECT_MODEL), "planning_per_request": rates.get("claude-sonnet"),
        "voice_per_1000_chars": rates.get("fal-elevenlabs"), "keyframe_per_image": rates.get("fal-nano-banana-pro"),
        "animation_per_second": min(animation, default=None),
    }


def generation_view(settings, library):
    assets = [dict(id=asset["id"], name=asset["name"], kind=asset["kind"], audio=bool(asset.get("audio")))
              for asset in library.assets.list_assets("kids_3d")]
    return {
        "generation": settings.get_generation(), "generation_assets": assets,
        "generation_rates": generation_rates(settings),
    }
