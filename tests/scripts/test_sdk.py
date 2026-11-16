import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO
import sdk


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout


class Checkout(unittest.TestCase):
    """A checkout of two repositories, src and src/v8, and this repository's patches for them."""

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


class Patch(Checkout):
    """step_patch through the states a kept checkout can be in."""

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


class Reconcile(Checkout):
    """step_reconcile on a build directory restored apart from its checkout, as CI's caches are."""

    def setUp(self):
        super().setUp()
        shutil.copy(REPO/'targets.json', self.repo/'targets.json')
        self.checkout = self.src.parent
        self.synced({'pin': {'tag': '1.0.0.0', 'commit': 'a'*40}, 'gclient': 'g'})
        self.write_patches('A\n')
        self.patch()
        self.old = 1_000_000_000_000_000_000
        for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h', self.src/'libchromium/BUILD.gn'):
            os.utime(path, ns=(self.old, self.old))

    def synced(self, record):
        (self.checkout/'.libchromium-checkout.json').write_text(json.dumps(record)+'\n')

    def build(self, target='linux-amd64', record=None):
        """A build directory built from the checkout as it is now, or with the record given."""
        out = sdk.out_dir(target)
        out.mkdir(parents=True, exist_ok=True)
        (out/'obj.o').write_text('')
        (out/sdk.BUILT_FROM).write_text(json.dumps(record if record is not None else sdk.built_from(target)))
        return out

    def reconcile(self, target='linux-amd64'):
        sdk.step_reconcile(target, None)

    def touched(self):
        return sorted(path.relative_to(self.src).as_posix()
                      for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h', self.src/'libchromium/BUILD.gn')
                      if path.stat().st_mtime_ns != self.old)

    def test_the_same_checkout_is_left_alone(self):
        out = self.build()
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), [])

    def test_no_build_directory(self):
        self.reconcile()
        self.assertFalse(sdk.out_dir('linux-amd64').exists())

    def test_another_patch_rebuilds_the_files_either_patch_changes(self):
        # Built with src's patch changing b.h; the checkout's changes a.h. v8's patch is the same.
        self.write_patches('a\n')
        (self.src/'b.h').write_text('B\n')
        (self.repo/'chromium/patches/src.patch').write_text(git(self.src, 'diff', '--', 'b.h'))
        (self.src/'b.h').write_text('b\n')
        self.patch()
        out = self.build()
        self.assertEqual(sdk.checkout_state()['patches']['']['files'], ['b.h'])
        # src's new patch changes a.h alone.
        sdk.unpatch(self.src)
        (self.src/'a.h').write_text('A\n')
        (self.repo/'chromium/patches/src.patch').write_text(git(self.src, 'diff', '--', 'a.h'))
        (self.src/'a.h').write_text('a\n')
        self.patch()
        self.assertEqual(sdk.checkout_state()['patches']['']['files'], ['a.h'])
        self.assertEqual((self.src/'b.h').read_text(), 'b\n')
        for path in (self.src/'a.h', self.src/'b.h', self.src/'v8/c.h'):
            os.utime(path, ns=(self.old, self.old))
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), ['a.h', 'b.h'])

    def test_another_gn_target_rebuilds_it(self):
        out = self.build()
        (self.src/'libchromium/BUILD.gn').write_text('# other\n')
        os.utime(self.src/'libchromium/BUILD.gn', ns=(self.old, self.old))
        self.reconcile()
        self.assertTrue((out/'obj.o').exists())
        self.assertEqual(self.touched(), ['libchromium/BUILD.gn'])

    def test_another_pin_builds_afresh(self):
        out = self.build()
        self.synced({'pin': {'tag': '2.0.0.0', 'commit': 'b'*40}, 'gclient': 'g'})
        self.reconcile()
        self.assertFalse(out.exists())
        self.assertEqual(self.touched(), [])

    def test_a_checkout_that_is_not_whole_builds_afresh(self):
        out = self.build()
        (self.checkout/'.libchromium-checkout.json').unlink()
        self.reconcile()
        self.assertFalse(out.exists())

    def test_a_build_directory_without_a_record_builds_afresh(self):
        out = self.build()
        (out/sdk.BUILT_FROM).unlink()
        self.reconcile()
        self.assertFalse(out.exists())

    def test_a_record_naming_files_outside_the_checkout_builds_afresh(self):
        record = sdk.built_from('linux-amd64')
        record['checkout']['patches']['']['files'] = ['../../outside']
        record['checkout']['patches']['']['sha256'] = 'other'
        outside = self.src.parent.parent/'outside'
        outside.write_text('')
        os.utime(outside, ns=(self.old, self.old))
        out = self.build(record=record)
        self.reconcile()
        self.assertFalse(out.exists())
        self.assertEqual(outside.stat().st_mtime_ns, self.old)

    def test_macos_built_with_another_apple_sdk_builds_afresh(self):
        marker = sdk.apple_sdk_marker()
        marker.parent.mkdir(parents=True)
        pinned = sdk.target_entry('macos-arm64')['mac_sdk']['sha256']
        for installed, kept in (('0'*64, False), (pinned, True)):
            with self.subTest(installed=installed):
                marker.write_text(installed)
                out = self.build('macos-arm64')
                # A restored checkout never holds the Apple SDK: mac-sdk installs the pinned one after.
                marker.unlink()
                self.reconcile('macos-arm64')
                self.assertEqual(out.exists(), kept)

    def test_configure_records_what_the_build_directory_is_built_from(self):
        (self.repo/'chromium/args.gn').write_text('is_debug = false\n')
        ran = []
        saved = sdk.run
        sdk.run = lambda *command, **kwargs: ran.append(command)
        try:
            sdk.step_configure('linux-amd64', None)
        finally:
            sdk.run = saved
        self.assertEqual(ran[0][:2], ('gn', 'gen'))
        out = sdk.out_dir('linux-amd64')
        self.assertEqual(json.loads((out/sdk.BUILT_FROM).read_text()), sdk.built_from('linux-amd64'))
        self.reconcile()
        self.assertEqual(self.touched(), [])


class Environment(unittest.TestCase):
    """What Chromium's tools run with."""

    def test_public_storage_is_read_without_signing_in(self):
        environment = sdk.env()
        self.assertEqual(environment['BOTO_CONFIG'], os.devnull)
        self.assertEqual(environment['SWARMING_HEADLESS'], '1')

    def test_the_callers_target_never_reaches_chromiums_tools(self):
        # build/rust/gni_impl/run_bindgen.py exits if it sees TARGET.
        saved = os.environ.get('TARGET')
        os.environ['TARGET'] = 'linux-amd64'
        try:
            self.assertNotIn('TARGET', sdk.env())
        finally:
            if saved is None:
                os.environ.pop('TARGET', None)
            else:
                os.environ['TARGET'] = saved


class DepotTools(unittest.TestCase):
    """depot_tools is cloned when missing, and its bootstrap always finished."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = Path(self.tmp.name)/'depot_tools'
        self.ran = []
        self.saved = os.environ.get('DEPOT_TOOLS'), sdk.run
        os.environ['DEPOT_TOOLS'] = str(self.tools)

        self.broken = None
        self.where = []

        def run(*command, cwd=None, check=True, **kwargs):
            self.ran.append([str(part) for part in command])
            self.where.append(cwd)
            if command[:2] == ('git', 'clone'):
                Path(command[-1]).mkdir(parents=True)
                (Path(command[-1])/'gclient').write_text('')
            if str(command[0]) == self.broken:
                if check:
                    raise subprocess.CalledProcessError(1, command)
                return 1
            return 0
        sdk.run = run

    def tearDown(self):
        tools, sdk.run = self.saved
        if tools is None:
            os.environ.pop('DEPOT_TOOLS', None)
        else:
            os.environ['DEPOT_TOOLS'] = tools
        self.tmp.cleanup()

    def test_a_fresh_clone_is_bootstrapped(self):
        sdk.ensure_depot_tools()
        self.assertEqual([command[0] for command in self.ran],
                         ['git', str(self.tools/'ensure_bootstrap'), str(self.tools/'python-bin/python3'),
                          str(self.tools/'vpython3')])

    def test_a_kept_clone_is_bootstrapped_again(self):
        self.tools.mkdir()
        (self.tools/'gclient').write_text('')
        (self.tools/'python3_bin_reldir.txt').write_text('bin\n')
        sdk.ensure_depot_tools()
        self.assertEqual(self.ran, [[str(self.tools/'ensure_bootstrap')],
                                    [str(self.tools/'python-bin/python3'), '-c', 'pass'],
                                    [str(self.tools/'vpython3'), '-c', 'pass']])
        # vpython3 takes depot_tools' own environment, not one around wherever the step runs.
        self.assertEqual(self.where[-1], self.tools)

    def test_a_bootstrap_that_left_no_python_fails(self):
        # ensure_bootstrap exits 0 even when it fetched nothing.
        for tool in ('python-bin/python3', 'vpython3'):
            with self.subTest(tool=tool):
                self.ran, self.broken = [], str(self.tools/tool)
                with self.assertRaises(subprocess.CalledProcessError):
                    sdk.ensure_depot_tools()

    def test_the_checkout_bootstraps_before_anything_else(self):
        calls = []
        saved = sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root
        sdk.ensure_depot_tools = lambda: calls.append('bootstrap')

        def wanted():
            calls.append('wanted')
            raise SystemExit('stop')
        sdk.wanted_checkout = wanted
        sdk.checkout_root = lambda: Path(self.tmp.name)/'checkout'
        try:
            with self.assertRaises(SystemExit):
                sdk.step_checkout('linux-amd64', None)
        finally:
            sdk.ensure_depot_tools, sdk.wanted_checkout, sdk.checkout_root = saved
        self.assertEqual(calls, ['bootstrap', 'wanted'])


if __name__ == '__main__':
    unittest.main()

