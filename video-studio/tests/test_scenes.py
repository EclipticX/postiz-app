import json
import unittest
from copy import deepcopy

from app.niche import load_profile
from app.scenes import (
    SCENE_SCHEMA,
    SceneValidationError,
    idea_prompt,
    planner_prompt,
    qa_prompt,
    validate_scene_plan,
)


class ScenePlanTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile()
        self.scenes = deepcopy(self.profile["few_shot"][0]["scenes"])

    def test_example_and_item_ids_pass_schema_and_timing_checks(self):
        self.scenes[0]["items"] = ["blue_watering_can"]
        self.scenes[1]["dialogue"] = ""
        self.scenes[1]["duration"] = 5

        actual = validate_scene_plan({"scenes": self.scenes}, self.profile, 9)

        self.assertEqual(actual, self.scenes)
        self.assertIn("items", SCENE_SCHEMA["properties"]["scenes"]["items"]["properties"])

    def test_collects_bad_references_duplicate_id_and_duration_mismatch(self):
        self.scenes[0]["characters"] = ["missing_character", "blue_watering_can"]
        self.scenes[0]["items"] = ["pebble_garden"]
        self.scenes[1]["scene_id"] = self.scenes[0]["scene_id"]
        self.scenes[1]["place"] = "tavi_seed_sprite"

        with self.assertRaises(SceneValidationError) as caught:
            validate_scene_plan({"scenes": self.scenes}, self.profile, 9)

        errors = str(caught.exception)
        self.assertIn("characters[0] references unknown asset missing_character", errors)
        self.assertIn("characters[1] must reference a character asset", errors)
        self.assertIn("items[0] must reference an item asset", errors)
        self.assertIn("scene_id duplicates scene_01", errors)
        self.assertIn("place must reference a place asset", errors)
        self.assertIn("add up to 8 seconds. Expected 9 seconds", errors)
        self.assertEqual(len(caught.exception.errors), 6)

    def test_rejects_malformed_scene_fields(self):
        self.scenes[0].pop("camera")
        self.scenes[0]["duration"] = 4.0
        self.scenes[0]["scene_id"] = "../file"
        self.scenes[0]["unrecognized"] = True
        self.scenes[1]["items"] = "blue_watering_can"

        with self.assertRaises(SceneValidationError) as caught:
            validate_scene_plan({"scenes": self.scenes}, self.profile, 8)

        errors = str(caught.exception)
        self.assertIn("missing camera", errors)
        self.assertIn("duration must be 4 or 5 whole seconds", errors)
        self.assertIn("scene_id must use only letters", errors)
        self.assertIn("unknown fields unrecognized", errors)
        self.assertIn("items must be an array", errors)

    def test_prompt_context_includes_few_shot_assets_and_validation_errors(self):
        planner = planner_prompt(self.profile, "Make a garden story", 8, ["Wrong place ID"])
        data = json.loads(planner.split("Inputs (JSON data)\n", 1)[1])
        self.assertEqual(data["approved_prompt"], "Make a garden story")
        self.assertEqual(data["target_duration_seconds"], 8)
        self.assertEqual(data["validation_errors_to_fix"], ["Wrong place ID"])
        self.assertEqual(data["output_schema"], SCENE_SCHEMA)
        self.assertEqual(data["niche"]["few_shot"], self.profile["few_shot"])
        self.assertNotIn("reference_image", data["niche"]["assets"][0])

    def test_idea_and_qa_context_include_stage_inputs(self):
        ideas = idea_prompt(self.profile, "Gardening", [{"hook": "A thirsty flower"}])
        idea_data = json.loads(ideas.split("Inputs (JSON data)\n", 1)[1])
        self.assertEqual(idea_data["trends"], "Gardening")
        self.assertEqual(idea_data["memory"], [{"hook": "A thirsty flower"}])

        qa = qa_prompt(self.profile, self.scenes)
        qa_data = json.loads(qa.split("Inputs (JSON data)\n", 1)[1])
        self.assertEqual(qa_data["approved_scenes"], self.scenes)
        self.assertIn("pass, review, or retry", qa)
