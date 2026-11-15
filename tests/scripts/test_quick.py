import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.scripts.common import REPO
import archive
import package
import publish

LINUX = {'target_os': 'linux', 'target_cpu': 'x64', 'sysroot': 'build/linux/debian_bullseye_amd64-sysroot'}
MAC = {'target_os': 'mac', 'target_cpu': 'arm64'}
TARGET = 'linux-amd64'


class Quick(unittest.TestCase):
    """CI's quick packages (specs/0003-ci-cd.md, CI) never reach a release."""

    def test_parts(self):
        self.assertEqual(package.parts_of(LINUX), ['lib', 'symbols', 'headers', 'toolchain', 'sysroot', 'notices'])
        self.assertEqual(package.parts_of(LINUX, quick=True), ['lib', 'headers', 'toolchain', 'sysroot', 'notices'])
        self.assertEqual(package.parts_of(MAC), ['lib', 'headers', 'toolchain', 'notices'])
        self.assertEqual(package.parts_of(MAC, quick=True), package.parts_of(MAC))

    def test_only_a_pull_request_s_version_is_quick(self):
        with tempfile.TemporaryDirectory() as tmp:
            for version in ('157.0.8103.39-1', '157.0.8103.39-12', '157.0.8103.39-pr', '157.0.8103.39-pr3-1'):
                with self.subTest(version=version), self.assertRaises(SystemExit):
                    package.package(TARGET, Path(tmp), Path(tmp), version, Path(tmp)/'out', quick=True)
            self.assertFalse((Path(tmp)/'out').exists())

    def test_a_release_is_at_the_release_level(self):
        self.assertEqual(archive.LEVEL, 19)
        self.assertLess(package.QUICK_LEVEL, archive.LEVEL)

    def test_publishing_refuses_a_quick_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            name = 'libchromium-1-linux-amd64-lib.tar.zst'
            (tmp/name).write_bytes(b'x')
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, {'tag': '1.2.3.4', 'commit': '9'*40},
                                       {name: {'target': TARGET, 'part': 'lib', 'quick': True}}, 'owner/name')

    def test_publishing_needs_every_part_of_every_target(self):
        targets = json.loads((REPO/'targets.json').read_text())
        whole = {f'{target}-{part}': {'target': target, 'part': part}
                 for target, entry in targets.items() for part in package.parts_of(entry)}
        publish.check_parts(whole, targets)
        for name in whole:
            with self.subTest(missing=name), self.assertRaises(SystemExit):
                publish.check_parts({other: entry for other, entry in whole.items() if other != name}, targets)
        quick = {f'{target}-{part}': {'target': target, 'part': part, 'quick': True}
                 for target, entry in targets.items() for part in package.parts_of(entry, quick=True)}
        with self.assertRaises(SystemExit):
            publish.check_parts(quick, targets)

    def test_a_quick_package_strips_and_links_its_libraries_as_a_release_does(self):
        def commands(symbols):
            ran = []
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(package.subprocess, 'run', lambda command, check: ran.append(command)):
                tmp = Path(tmp)
                libraries = [tmp/'lib/libchromium.so', tmp/'lib/libffmpeg.so']
                package.strip_libraries(libraries, tmp/'toolchain', 'linux', tmp/symbols/'symbols')
                self.assertTrue((tmp/symbols/'symbols').is_dir())
                # The same commands but for where the debug information goes, whose name the link carries.
                return [[part.replace(str(tmp/symbols), '<symbols>').replace(str(tmp), '<tmp>') for part in command]
                        for command in ran]

        release = commands('stage/symbols')
        self.assertEqual(commands('stage/debug'), release)
        self.assertEqual(release[2], ['<tmp>/toolchain/bin/llvm-objcopy', '--add-gnu-debuglink=<symbols>/symbols/'
                                      'libchromium.so.debug', '<tmp>/lib/libchromium.so'])
        self.assertEqual(len(release), 6)


if __name__ == '__main__':
    unittest.main()
