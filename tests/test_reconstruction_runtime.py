"""Synthetic tests: no original game data or model processes required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from generator_launcher.contracts import ContractError, load_or_create_installation
from generator_launcher.local_preparation import preparation_receipt
from generator_launcher.reconstruction_runtime import reconstruct_local_manifest
from generator_launcher.text_reconstruction import export_recipe


class LocalTextReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name).resolve()
        self.root = self.work / 'MI1 Generator'
        self.root.mkdir()
        self.installation = load_or_create_installation(self.root, config_path=self.work / "installation.json")
        self.config = {'project_state': {'project_id': 'synthetic-project',
            'version': {'manifest_sha256': 'synthetic-recipe'}}}
        self.full = {'manifest_id': 'synthetic', 'roles': [], 'dialogues': [{
            'dialog_id': 'synthetic:8', 'output_stem': '8',
            'original_text': {'text': 'A fictional copper lantern.'},
            'text_variants': []}]}
        self.recipe = export_recipe(self.full, {'en': {'8': 'A fictional copper lantern.'}})
        outputs = {}
        for locale in ('english', 'german'):
            directory = self.work / locale
            directory.mkdir()
            for name in ('monkey.000', 'monkey.001'):
                path = directory / name
                path.write_bytes((locale + name).encode())
                outputs[locale + ':' + name] = self.artifact(path)
        # A minimal valid SOU/VCTL/VOC container, with a real scanner index.
        archive = self.work / 'english/monster.sou'
        archive.write_bytes(b'SOU ' + bytes(4) + b'VCTL' + (8).to_bytes(4, 'big') + b'Creative Voice File\x1a')
        outputs['english:monster.sou'] = self.artifact(archive)
        inputs = {'selected_manifest_sha256': 'synthetic-recipe'}
        receipt = preparation_receipt(self.installation, project_id='synthetic-project',
            inputs=inputs, outputs=outputs)
        path = self.root / 'generated/game-preparation/preparation.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'preparation_receipt': receipt}))
        self.receipt_path = path
        control = r'\xFF\x0A\x08\x00\xFF\x0A\x00\x00\xFF\x0A\x08\x00\xFF\x0A\x00\x00'
        export = '[1:SCRP#1](14:A1)' + control + 'A fictional copper lantern.\n'
        tool = self.root / 'bin/scummtr'
        tool.parent.mkdir()
        tool.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n'
            + 'Path(sys.argv[sys.argv.index("-f")+1]).write_text(' + repr(export)
            + ', encoding="cp1252")\n')
        tool.chmod(0o755)

    def artifact(self, path):
        return {'path': path.relative_to(self.work).as_posix(),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def tearDown(self):
        self.temporary.cleanup()

    def test_authenticated_games_reconstruct_and_remove_private_exports(self):
        actual = reconstruct_local_manifest(self.root, self.recipe, self.config, self.installation)
        self.assertEqual(actual, self.full)
        self.assertEqual(list((self.root / 'generated/text-reconstruction').iterdir()), [])

    def test_changed_game_and_different_manifest_are_rejected(self):
        (self.work / 'german/monkey.001').write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError, 'authentifiziert'):
            reconstruct_local_manifest(self.root, self.recipe, self.config, self.installation)

    def test_preparation_from_other_recipe_is_rejected(self):
        self.config['project_state']['version']['manifest_sha256'] = 'different'
        with self.assertRaisesRegex(ContractError, 'anderen Manifest'):
            reconstruct_local_manifest(self.root, self.recipe, self.config, self.installation)

    def test_private_full_manifest_needs_no_export(self):
        self.assertIs(reconstruct_local_manifest(self.root, self.full, {}, None), self.full)

    def test_private_export_destination_cannot_escape_through_symlink(self):
        outside = self.work / 'outside'
        outside.mkdir()
        (self.root / 'generated/text-reconstruction').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError):
            reconstruct_local_manifest(self.root, self.recipe, self.config, self.installation)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
