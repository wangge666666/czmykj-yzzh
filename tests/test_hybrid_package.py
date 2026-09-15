import hashlib
import json
import tempfile
import unittest
import zipfile
import subprocess
import sys
from pathlib import Path, PureWindowsPath, PurePosixPath
import runpy
import os

from scripts.build_hybrid_plugin import build


class PackageTests(unittest.TestCase):
    def test_new_video_workflows_import_and_serve_from_the_extracted_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            archive = build(Path(root) / 'new-workflows.zip')
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root)
            runtime = Path(root) / 'czmiyou-yzzh' / 'runtime'
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
            env.pop('PYTHONPATH', None)
            script = '''import web_app
client = web_app.app.test_client()
for page in ('/projects/wardrobe', '/projects/motion-transfer', '/projects/wardrobe-continuation'):
    response = client.get(page)
    assert response.status_code == 200, page
print('bundled workflows imported and served')
'''
            result = subprocess.run([sys.executable, '-c', script], cwd=runtime, env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('bundled workflows imported and served', result.stdout)

    def test_template_must_be_empty_even_if_inventory_is_rehashed(self):
        with tempfile.TemporaryDirectory() as root:
            archive=build(Path(root)/'customer.zip')
            with zipfile.ZipFile(archive) as bundle:bundle.extractall(root)
            plugin=Path(root)/'czmiyou-yzzh'
            template=plugin/'.env.example'
            template.write_text('ARK_API_KEY=fixture-not-empty\n')
            inventory=json.loads((plugin/'SHA256SUMS.json').read_text())
            inventory['.env.example']=hashlib.sha256(template.read_bytes()).hexdigest()
            (plugin/'SHA256SUMS.json').write_text(json.dumps(inventory))
            result=subprocess.run([sys.executable,str(plugin/'scripts/validate_release.py')],capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_release_names_are_portable_between_windows_and_posix(self):
        validator = Path(__file__).resolve().parents[1] / 'plugins/czmiyou-yzzh/scripts/validate_release.py'
        relative_name = runpy.run_path(str(validator))['relative_name']
        self.assertEqual(relative_name(PureWindowsPath('C:/bundle/runtime/yzzh_local/app.py'),
                                       PureWindowsPath('C:/bundle')), 'runtime/yzzh_local/app.py')
        self.assertEqual(relative_name(PurePosixPath('/bundle/runtime/yzzh_local/app.py'),
                                       PurePosixPath('/bundle')), 'runtime/yzzh_local/app.py')

    def test_customer_archive_is_allowlisted_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as root:
            archive = build(Path(root) / 'customer.zip')
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                inventory = json.loads(bundle.read('czmiyou-yzzh/SHA256SUMS.json'))
                self.assertTrue(any(x.endswith('yzzh_local/mcp.py') for x in names))
                self.assertIn('czmiyou-yzzh/.env.example', names)
                self.assertNotIn('czmiyou-yzzh/.env', names)
                for forbidden in ('yzzh_cloud/', '.env.production', '.git/', 'connection.json', 'state.sqlite3', '.venv/', '__pycache__/', 'runs/'):
                    self.assertFalse(any(forbidden in x for x in names), forbidden)
                for name, expected in inventory.items():
                    self.assertEqual(hashlib.sha256(bundle.read('czmiyou-yzzh/' + name)).hexdigest(), expected)
                requirements = bundle.read('czmiyou-yzzh/requirements-local.txt').decode().lower()
                self.assertNotIn('demucs==', requirements)
                self.assertNotIn('torch==', requirements)
                self.assertIn('czmiyou-yzzh/skills/yzzh/agents/openai.yaml', names)
            with self.assertRaises(FileExistsError): build(archive)

    def test_extracted_release_validator_detects_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            archive = build(Path(root) / 'customer.zip')
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(Path(root) / 'extracted')
            plugin = Path(root) / 'extracted' / 'czmiyou-yzzh'
            command = [sys.executable, str(plugin / 'scripts' / 'validate_release.py')]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (plugin / 'runtime' / 'yzzh_local' / 'mcp.py').write_text('tampered fixture')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_release_rejects_changed_structure_even_with_matching_inventory(self):
        for missing in (True, False):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as root:
                archive = build(Path(root) / 'customer.zip')
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(Path(root) / 'extracted')
                plugin = Path(root) / 'extracted' / 'czmiyou-yzzh'
                inventory_path = plugin / 'SHA256SUMS.json'
                inventory = json.loads(inventory_path.read_text())
                if missing:
                    name = 'runtime/yzzh_local/runtime.py'
                    (plugin / name).unlink()
                    del inventory[name]
                else:
                    name = 'unexpected.txt'
                    (plugin / name).write_bytes(b'extra fixture')
                    inventory[name] = hashlib.sha256(b'extra fixture').hexdigest()
                inventory_path.write_text(json.dumps(inventory))
                result = subprocess.run([sys.executable, str(plugin / 'scripts' / 'validate_release.py')], capture_output=True)
                self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
