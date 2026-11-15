import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import sdk


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout


class Patch(unittest.TestCase):
    """step_patch on a checkout of two repositories, src and src/v8, through the states a kept disk can be in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root/'repo'
        self.src = root/'checkout/src'
        for where in (self.src, self.src/'v8'):
            where.mkdir(parents=True)
            git(where, 'init', '-q')
            git(where, 'config', 'user.email', 't@example.com')
            git(where, 'config', 'user.name', 'T')
            git(where, 'config', 'commit.gpgsign', 'false')
        (self.src/'a.h').write_text('a\n')
        (self.src/'b.h').write_text('b\n')
        (self.src/'.gitignore').write_text('v8/\nlibchromium/\n')
        git(self.src, 'add', '.')
        git(self.src, 'commit', '-qm', 'pin')
        (self.src/'v8/c.h').write_text('c\n')
        git(self.src/'v8', 'add', '.')
        git(self.src/'v8', 'commit', '-qm', 'pin')
        (self.repo/'chromium/gn').mkdir(parents=True)
        (self.repo/'chromium/gn/BUILD.gn').write_text('# gn\n')
        self.saved = sdk.REPO, os.environ.get('LIBCHROMIUM_CHECKOUT')
        sdk.REPO = self.repo
        os.environ['LIBCHROMIUM_CHECKOUT'] = str(root/'checkout')

    def tearDown(self):
        sdk.REPO = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop('LIBCHROMIUM_CHECKOUT', None)
        else:
            os.environ['LIBCHROMIUM_CHECKOUT'] = self.saved[1]
        self.tmp.cleanup()

    def write_patches(self, src_text, v8_text='C\n'):
        """Patches made the way export_patch.py makes them: edit, diff, restore."""
        patches = self.repo/'chromium/patches'
        shutil.rmtree(patches, ignore_errors=True)
        patches.mkdir(parents=True)
        for where, name, text, out in ((self.src, 'a.h', src_text, patches/'src.patch'),
                                       (self.src/'v8', 'c.h', v8_text, patches/'v8.patch')):
            original = (where/name).read_text()
            (where/name).write_text(text)
            out.write_text(git(where, 'diff'))
            (where/name).write_text(original)

    def patch(self):
        sdk.step_patch('linux-amd64', None)

    def test_applies_once(self):
        self.write_patches('A\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')
        before = (self.src/'a.h').stat().st_mtime_ns
        self.patch()
        self.assertEqual((self.src/'a.h').stat().st_mtime_ns, before)
        self.assertEqual((self.src/'libchromium/BUILD.gn').read_text(), '# gn\n')

    def test_resumes_between_repositories(self):
        self.write_patches('A\n')
        # A machine stopped after src's patch and its record, before v8's.
        git(self.src, 'apply', str(self.repo/'chromium/patches/src.patch'))
        sdk.record('', self.repo/'chromium/patches/src.patch')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')

    def test_a_patch_applied_without_a_record(self):
        self.write_patches('A\n')
        git(self.src, 'apply', str(self.repo/'chromium/patches/src.patch'))
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')

    def test_a_half_applied_patch(self):
        self.write_patches('A\n')
        (self.src/'a.h').write_text('garbled\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'A\n')

    def test_a_changed_patch_replaces_the_old(self):
        self.write_patches('A\n')
        self.patch()
        self.write_patches('AA\n')
        v8_time = (self.src/'v8/c.h').stat().st_mtime_ns
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'AA\n')
        self.assertEqual((self.src/'v8/c.h').read_text(), 'C\n')
        # An unchanged file keeps its time, so nothing that includes it rebuilds.
        self.assertEqual((self.src/'v8/c.h').stat().st_mtime_ns, v8_time)

    def test_stopped_mid_apply_then_the_patch_drops_a_file(self):
        # A patch of a.h and b.h, recorded, then a machine stopped after changing a.h only.
        self.write_patches('A\n')
        patch = self.repo/'chromium/patches/src.patch'
        (self.src/'b.h').write_text('B\n')
        two = git(self.src, 'diff', '--', 'b.h')
        (self.src/'b.h').write_text('b\n')
        patch.write_text(patch.read_text()+two)
        sdk.record('', patch)
        (self.src/'a.h').write_text('A\n')
        # The next push's patch changes b.h alone.
        (self.src/'a.h').write_text('a\n')
        (self.src/'b.h').write_text('BB\n')
        patch.write_text(git(self.src, 'diff', '--', 'b.h'))
        (self.src/'b.h').write_text('b\n')
        (self.src/'a.h').write_text('A\n')
        self.patch()
        self.assertEqual((self.src/'a.h').read_text(), 'a\n')
        self.assertEqual((self.src/'b.h').read_text(), 'BB\n')

    def test_a_dropped_patch_is_reverted(self):
        self.write_patches('A\n')
        self.patch()
        (self.repo/'chromium/patches/v8.patch').unlink()
        self.patch()
        self.assertEqual((self.src/'v8/c.h').read_text(), 'c\n')
        sdk.unpatch(self.src)
        self.assertEqual((self.src/'a.h').read_text(), 'a\n')


class Environment(unittest.TestCase):
    """What Chromium's tools run with."""

    def test_public_storage_is_read_without_signing_in(self):
        environment = sdk.env()
        self.assertEqual(environment['BOTO_CONFIG'], os.devnull)
        self.assertEqual(environment['SWARMING_HEADLESS'], '1')


if __name__ == '__main__':
    unittest.main()

