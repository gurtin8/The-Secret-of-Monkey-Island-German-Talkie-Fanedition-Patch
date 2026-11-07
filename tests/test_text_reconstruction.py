import copy
import hashlib
import json
import unittest

from generator_launcher.text_reconstruction import (
    MARKER,
    ReconstructionError,
    export_recipe,
    reconstruct,
)


def _manifest():
    manifest = {
        "manifest_id": "fictional:test",
        "manifest_revision": 3,
        "game": {"title": "Fictional Sample"},
        "roles": [{
            "role_id": "role:captain",
            "main_anchor": {
                "anchor_component_dialog_ids": ["dlg:1", "dlg:2"],
                "prompt_text": "A bright lantern! Keep the map dry.",
            },
            "prewarm": {"source": "neutral_standard", "text": "Ich bin bereit."},
        }],
        "dialogues": [{
            "dialog_id": "dlg:1", "output_stem": "1",
            "original_text": {"text": "A bright lantern!"},
            "text_variants": [{
                "variant_id": "dlg:1:de", "display_text": "Eine helle Laterne!",
                "tts_text": "Eine helle Laterne!", "qa_expected_text": "Eine helle Laterne!",
                "qa_allowed_variants": [],
                "sha256": "stale",
                "normalization_steps": [{"before": "Eine Helle Laterne!", "after": "Eine helle Laterne!"}],
            }],
        }, {
            "dialog_id": "dlg:2", "output_stem": "2",
            "original_text": {"text": "Keep the map dry."},
            "text_variants": [],
        }],
        "source_evidence": {"launcher_preparation": {
            "roles": [{"role_id": "role:captain", "reference": {
                "source_dialog_ids": ["dlg:1", "dlg:2"],
                "prompt_text": "A bright lantern! Keep the map dry.",
            }}],
            "sources": [
                {"dialog_id": "dlg:1", "native_speech_id": "1", "archive_member": "fictional.sou"},
                {"dialog_id": "dlg:2", "native_speech_id": "2", "archive_member": "fictional.sou"},
            ],
        }},
    }
    variant = manifest["dialogues"][0]["text_variants"][0]
    payload = [variant[k] for k in ("display_text", "tts_text", "qa_expected_text", "qa_allowed_variants")]
    variant["sha256"] = _canonical_sha(payload)
    return manifest


def _indexes():
    return {
        "en": {
            "1": {"text": "A bright lantern!", "location": {"archive_member": "fictional.sou"}},
            "2": {"text": "Keep the map dry.", "location": {"archive_member": "fictional.sou"}},
        },
        "de": {"1": "Eine Helle Laterne!"},
    }


def _canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class TextReconstructionTests(unittest.TestCase):
    def test_textless_recipe_round_trips_and_keeps_only_delta_insertions(self):
        original = _manifest()
        recipe = export_recipe(original, _indexes())
        encoded = json.dumps(recipe, ensure_ascii=False)
        self.assertNotIn("A bright lantern!", encoded)
        self.assertNotIn("Keep the map dry.", encoded)
        self.assertNotIn("Eine Helle Laterne!", encoded)
        self.assertIn("Ich bin bereit.", encoded)  # fixed neutral bank copy
        self.assertTrue(all(not slot["path"].endswith("prewarm/text")
                            for slot in recipe[MARKER]["slots"]))

        restored = reconstruct(recipe, _indexes())
        self.assertEqual(restored, original)
        self.assertNotIn(MARKER, restored)

    def test_wrong_base_and_ambiguous_source_are_rejected(self):
        recipe = export_recipe(_manifest(), _indexes())
        wrong = copy.deepcopy(_indexes())
        wrong["de"]["1"] = "A different local line."
        with self.assertRaisesRegex(ReconstructionError, "Ausgangstext"):
            reconstruct(recipe, wrong)

        ambiguous = copy.deepcopy(_indexes())
        ambiguous["en"]["1"] = [
            {"text": "A bright lantern!", "location": {"room_id": 4}},
            {"text": "A bright lantern!", "location": {"room_id": 5}},
        ]
        with self.assertRaisesRegex(ReconstructionError, "Mehrdeutige"):
            reconstruct(recipe, ambiguous)

    def test_mutated_target_hash_and_repeated_dialogue_ids_are_rejected(self):
        recipe = export_recipe(_manifest(), _indexes())
        bad_target = copy.deepcopy(recipe)
        bad_target[MARKER]["slots"][0]["expected_target_sha256"] = "0" * 64
        with self.assertRaisesRegex(ReconstructionError, "Zieltext"):
            reconstruct(bad_target, _indexes())

        repeated = _manifest()
        repeated["dialogues"].append(copy.deepcopy(repeated["dialogues"][0]))
        with self.assertRaisesRegex(ReconstructionError, "Dialog-IDs"):
            reconstruct(export_recipe(repeated, _indexes()), _indexes())

    def test_full_manifest_digest_is_checked(self):
        recipe = export_recipe(_manifest(), _indexes())
        recipe[MARKER]["expected_manifest_sha256"] = "f" * 64
        with self.assertRaisesRegex(ReconstructionError, "Referenzhash"):
            reconstruct(recipe, _indexes())


if __name__ == "__main__":
    unittest.main()
