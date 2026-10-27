"""The Mac download carries one independent executable with its execute bit."""

import importlib.util
import os
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_macos', ROOT / 'packaging/prepare_macos.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MacDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.bundle = self.base / 'bundle'
        (self.bundle / '_internal').mkdir(parents=True)
        self.executable = self.bundle / 'beancount-budget-tool'
        self.executable.write_bytes(b'console executable placeholder')
        self.executable.chmod(0o755)
        (self.bundle / '_internal/libexample.dylib').write_bytes(b'native dependency')
        self.output = self.base / 'downloads'
        self.revision = 'a' * 40

    def package(self):
        return module.prepare(self.executable, self.output, 'arm64', self.revision)

    def test_download_has_offline_guides_example_revision_and_execute_permission(self):
        archive = self.package()
        name = 'beancount-budget-tool-macos-arm64'
        with tarfile.open(archive, 'r:gz') as tar:
            members = {member.name: member for member in tar}
            self.assertFalse(any('/_internal' in path for path in members))
            self.assertEqual(tar.extractfile(name + '/beancount-budget-tool').read(), self.executable.read_bytes())
            if os.name != 'nt':
                self.assertTrue(members[name + '/beancount-budget-tool'].mode & 0o111)
            self.assertEqual(tar.extractfile(name + '/BUILD-COMMIT.txt').read().decode().strip(), self.revision)
            start = tar.extractfile(name + '/START-HERE.md').read().decode('utf-8')
            self.assertIn('(QUICKSTART.md)', start)
            self.assertIn('(docs/CONFIGURATION.md)', start)
            self.assertNotIn('../', start)
            for relative in ('QUICKSTART.md', 'docs/CONFIGURATION.md', 'example/simple/config.yml', 'LICENSE'):
                self.assertIn(name + '/' + relative, members)
            self.assertFalse(any('.budget-state' in path or 'private-ledger' in path for path in members))
            self.assertIn(name + '/docs/ARCHITECTURE.md', members)
            notice = tar.extractfile(name + '/LEGACY-WORKFLOWS.md').read().decode('utf-8')
            self.assertIn('旧操作入口已下线', notice)
            self.assertNotIn('beancount-budget-init --apply', notice)
            self.assertNotIn(name + '/example/first-month-setup.yaml', members)

    def test_old_bundle_directory_is_rejected_instead_of_shipping_dependencies(self):
        with self.assertRaises(ValueError):
            module.prepare(self.bundle, self.output, 'arm64', self.revision)
        self.assertFalse(self.output.exists())

    def test_missing_executable_and_existing_archive_are_never_overwritten(self):
        self.executable.unlink()
        with self.assertRaises(ValueError):
            self.package()
        self.assertFalse(self.output.exists())
        self.executable.write_bytes(b'executable')
        archive = self.package()
        before = archive.read_bytes()
        with self.assertRaises(FileExistsError):
            self.package()
        self.assertEqual(archive.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
