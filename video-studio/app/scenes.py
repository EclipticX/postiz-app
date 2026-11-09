import json
import re
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SCENE_SCHEMA = json.loads((APP_DIR / "scene.schema.json").read_text(encoding="utf-8"))
SCENE_DEFINITION = SCENE_SCHEMA["properties"]["scenes"]["items"]
SCENE_FIELDS = SCENE_DEFINITION["properties"]
SCENE_DURATIONS = SCENE_FIELDS["duration"]["enum"]


class SceneValidationError(ValueError):
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


def validate_scene_plan(document, profile: dict, total_duration: int) -> list[dict]:
    if type(total_duration) is not int or total_duration <= 0:
        raise SceneValidationError(["Target duration must be a positive number of whole seconds"])
    if not isinstance(document, dict):
        raise SceneValidationError(["Return a JSON object with a scenes array"])
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise SceneValidationError(["scenes must be a nonempty array"])

    errors = []
    if document.keys() - {"scenes"}:
        errors.append("The top-level object can contain only scenes")

    assets = {asset["id"]: asset["kind"] for asset in profile["assets"]}
    voice_styles = {asset["id"]: {voice["id"] for voice in asset.get("voice_styles", [])} for asset in profile["assets"]}
    scene_ids = set()
    durations = []

    for index, scene in enumerate(scenes):
        location = f"scenes[{index}]"
        if not isinstance(scene, dict):
            errors.append(f"{location} must be an object")
            continue

        missing = [name for name in SCENE_DEFINITION["required"] if name not in scene]
        if missing:
            errors.append(f"{location} is missing {', '.join(missing)}")
        extra = scene.keys() - SCENE_FIELDS.keys()
        if extra:
            errors.append(f"{location} has unknown fields {', '.join(sorted(map(str, extra)))}")

        for field in ("scene_id", "action", "camera", "dialogue"):
            if field not in scene:
                continue
            value = scene[field]
            if not isinstance(value, str) or (field != "dialogue" and not value.strip()):
                requirement = "a string" if field == "dialogue" else "nonempty text"
                errors.append(f"{location}.{field} must be {requirement}")
            elif field == "scene_id":
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
                    errors.append(
                        f"{location}.scene_id must use only letters, digits, underscores, or hyphens "
                        "and start with a letter or digit. Use at most 64 characters"
                    )
                elif value in scene_ids:
                    errors.append(f"{location}.scene_id duplicates {value}")
                scene_ids.add(value)

        if "place" in scene:
            _validate_asset(scene["place"], "place", f"{location}.place", assets, errors)
        for field, kind in (("characters", "character"), ("items", "item")):
            if field not in scene:
                continue
            references = scene[field]
            if not isinstance(references, list):
                errors.append(f"{location}.{field} must be an array of {kind} asset IDs")
                continue
            for position, reference in enumerate(references):
                _validate_asset(reference, kind, f"{location}.{field}[{position}]", assets, errors)

        _validate_dialogue_lines(scene, location, voice_styles, errors)

        if "duration" in scene:
            duration = scene["duration"]
            if type(duration) is not int or duration not in SCENE_DURATIONS:
                errors.append(f"{location}.duration must be 4 or 5 whole seconds")
            else:
                durations.append(duration)

    actual_duration = sum(durations)
    if len(durations) == len(scenes) and actual_duration != total_duration:
        errors.append(f"Scene durations add up to {actual_duration} seconds. Expected {total_duration} seconds")
    if errors:
        raise SceneValidationError(errors)
    return scenes


def spoken_lines(scene):
    if scene.get("dialogue_lines"):
        return scene["dialogue_lines"]
    text = scene.get("dialogue", "").strip()
    return [{"text": text}] if text else []


def _validate_dialogue_lines(scene, location, voice_styles, errors):
    if "dialogue_lines" not in scene:
        return
    lines = scene["dialogue_lines"]
    if not isinstance(lines, list) or len(lines) > 3:
        errors.append(f"{location}.dialogue_lines must be an array of at most three short lines")
        return
    if lines and isinstance(scene.get("dialogue"), str) and scene["dialogue"].strip():
        errors.append(f"{location}.dialogue must be empty when dialogue_lines are used")
    characters = scene.get("characters")
    characters = characters if isinstance(characters, list) else []
    for index, line in enumerate(lines):
        field = f"{location}.dialogue_lines[{index}]"
        if not isinstance(line, dict) or line.keys() - {"speaker_id", "text", "voice_style_id"}:
            errors.append(f"{field} must contain a speaker ID, text, and an optional voice style ID")
            continue
        speaker, content = line.get("speaker_id"), line.get("text")
        if not isinstance(speaker, str) or speaker not in characters or speaker not in voice_styles:
            errors.append(f"{field}.speaker_id must reference a character in this scene")
        elif "voice_style_id" in line and (
            not isinstance(line["voice_style_id"], str) or line["voice_style_id"] not in voice_styles[speaker]
        ):
            errors.append(f"{field}.voice_style_id must reference a voice style for {speaker}")
        if not isinstance(content, str) or not content.strip():
            errors.append(f"{field}.text must be nonempty text")


def _validate_asset(reference, kind, location, assets, errors):
    article = "an" if kind == "item" else "a"
    if not isinstance(reference, str) or not reference.strip():
        errors.append(f"{location} must be {article} {kind} asset ID")
    elif reference not in assets:
        errors.append(f"{location} references unknown asset {reference}")
    elif assets[reference] != kind:
        errors.append(f"{location} must reference {article} {kind} asset. {reference} has kind {assets[reference]}")


def planner_prompt(profile: dict, prompt: str, total_duration: int, errors=()) -> str:
    return _prompt(
        "planner",
        {
            "niche": _niche_context(profile),
            "approved_prompt": prompt,
            "target_duration_seconds": total_duration,
            "output_schema": SCENE_SCHEMA,
            "validation_errors_to_fix": list(errors),
        },
    )


def idea_prompt(profile: dict, trends, memory) -> str:
    return _prompt("ideas", {"niche": _niche_context(profile), "trends": trends, "memory": memory})


def qa_prompt(profile: dict, scenes: list[dict]) -> str:
    return _prompt("vision_qa", {"niche": _niche_context(profile), "approved_scenes": scenes})


def _prompt(name: str, context: dict) -> str:
    instructions = (APP_DIR / "prompts" / f"{name}.txt").read_text(encoding="utf-8").strip()
    return instructions + "\n\nInputs (JSON data)\n" + json.dumps(context, ensure_ascii=False, indent=2)


def _niche_context(profile: dict) -> dict:
    context = {
        key: profile[key]
        for key in ("id", "name", "description", "style_guide", "script_style", "audience_rules", "brand_rules", "sample_prompt", "few_shot")
        if key in profile
    }
    context["assets"] = []
    for asset in profile["assets"]:
        entry = {key: asset[key] for key in ("id", "kind", "name", "description") if key in asset}
        if asset.get("voice_styles"):
            entry["voice_styles"] = [{"id": voice["id"], "name": voice["name"]} for voice in asset["voice_styles"]]
        if asset.get("selected_voice"):
            entry["preferred_voice_style_id"] = asset["selected_voice"]["id"]
        context["assets"].append(entry)
    return context
