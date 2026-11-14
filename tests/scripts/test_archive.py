import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import archive


@unittest.skipUnless(shutil.which('zstd'), 'needs zstd')
class Deterministic(unittest.TestCase):
    def test_same_files_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ('a', 'b'):
                root = tmp/name
                (root/'lib').mkdir(parents=True)
                (root/'lib/x.so').write_bytes(b'\x7fELF')
                (root/'lib/x.so').chmod(0o755)
                (root/'z.txt').write_text('z')
                (root/'link').symlink_to('z.txt')
            (tmp/'b/z.txt').touch()  # a different time
            archive.write(tmp/'a', tmp/'a.tar.zst')
            archive.write(tmp/'b', tmp/'b.tar.zst')
            self.assertEqual((tmp/'a.tar.zst').read_bytes(), (tmp/'b.tar.zst').read_bytes())
            listing = subprocess.check_output(['tar', '--zstd', '-tvf', str(tmp/'a.tar.zst')], text=True)
            self.assertIn('lib/x.so', listing)
            self.assertIn('link -> z.txt', listing)

    def test_members_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ('b/2', 'a/1', 'c'):
                (tmp/name).parent.mkdir(parents=True, exist_ok=True)
                (tmp/name).write_text('x')
            self.assertEqual(list(archive.members(tmp)), ['c', 'a', 'a/1', 'b', 'b/2'])


if __name__ == '__main__':
    unittest.main()
