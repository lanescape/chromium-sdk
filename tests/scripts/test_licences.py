import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import licences

DSC = """Format: 3.0 (quilt)
Source: glibc
Version: 2.31-13+deb11u6
Checksums-Sha256:
 aaaa 100 glibc_2.31.orig.tar.xz
 bbbb 20 glibc_2.31-13+deb11u6.debian.tar.xz
Files:
 cccc 100 glibc_2.31.orig.tar.xz
"""


class Debian(unittest.TestCase):
    def test_checksums(self):
        self.assertEqual(licences.checksums(DSC), [('glibc_2.31.orig.tar.xz', 'aaaa'),
                                                   ('glibc_2.31-13+deb11u6.debian.tar.xz', 'bbbb')])

    def test_pool_path(self):
        self.assertEqual(licences.pool_path('glibc', '2.31-13+deb11u6'),
                         'pool/main/g/glibc/glibc_2.31-13+deb11u6.dsc')
        self.assertEqual(licences.pool_path('libx11', '2:1.7.2-1'), 'pool/main/libx/libx11/libx11_1.7.2-1.dsc')

    def test_sysroot_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for package, control in (
                    ('libc6', 'Package: libc6\nSource: glibc\nVersion: 2.31-13+deb11u6\n'),
                    ('libc6-dev', 'Package: libc6-dev\nSource: glibc\nVersion: 2.31-13+deb11u6\n'),
                    ('zlib1g', 'Package: zlib1g\nSource: zlib (1:1.2.11.dfsg-2+deb11u2)\nVersion: 1:1.2.11.dfsg-2+deb11u2+b1\n'),
                    ('libacl1', 'Package: libacl1\nVersion: 2.2.53-10\n')):
                (tmp/'debian'/package/'DEBIAN').mkdir(parents=True)
                (tmp/'debian'/package/'DEBIAN/control').write_text(control)
            self.assertEqual(licences.sysroot_packages(tmp), [('glibc', '2.31-13+deb11u6'), ('libacl1', '2.2.53-10'),
                                                              ('zlib', '1:1.2.11.dfsg-2+deb11u2')])

    def test_snapshot_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp)/'bullseye.amd64'
            listing.write_text('https://snapshot.debian.org/archive/debian/20250129T203412Z/pool/main/a/acl/x.deb\n')
            self.assertEqual(licences.snapshot_base(listing),
                             'https://snapshot.debian.org/archive/debian/20250129T203412Z/')


class Copyleft(unittest.TestCase):
    def test_licences_that_ask_for_source(self):
        for name in ('LGPL-2.1', 'MPL-2.0', 'bsd-2-clause, lgpl-2.1, mit', 'BSD and LGPL v2 and LGPL v2.1',
                     'GPL-2.0', 'EPL-1.0'):
            self.assertTrue(licences.COPYLEFT.search(name), name)
        for name in ('BSD-3-Clause', 'Apache-2.0', 'MIT', 'ISC', 'Zlib', 'FFT2D', 'Apache-2.0 WITH LLVM-exception'):
            self.assertFalse(licences.COPYLEFT.search(name), name)

    def test_slug(self):
        self.assertEqual(licences.slug('third_party/ffmpeg'), 'third-party-ffmpeg')
        self.assertEqual(licences.slug('third_party/rust/chromium_crates_io/vendor/symphonia-core-v0_6'),
                         'third-party-rust-chromium-crates-io-vendor-symphonia-core-v0-6')


if __name__ == '__main__':
    unittest.main()
