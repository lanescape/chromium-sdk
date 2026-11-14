import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import version


class Versions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'tag.gpgsign', 'false')
        self.saved = version.REPO
        version.REPO = self.repo

    def tearDown(self):
        version.REPO = self.saved
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, text=True,
                              stdout=subprocess.PIPE).stdout.strip()

    def commit(self, tag='157.0.8103.39', message='change'):
        (self.repo/'chromium').mkdir(exist_ok=True)
        (self.repo/'chromium/pin.json').write_text(json.dumps({'tag': tag, 'commit': '0'*40}))
        (self.repo/'note').write_text(message+self.git('rev-parse', '--short', 'HEAD') if (self.repo/'note').exists()
                                      else message)
        self.git('add', '.')
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def test_first_version_of_a_pin_is_1(self):
        self.commit()
        answer = version.for_main()
        self.assertTrue(answer['publish'])
        self.assertEqual(answer['version'], '157.0.8103.39-1')
        self.assertEqual(answer['tag'], 'v157.0.8103.39-1')
        self.assertIsNone(answer['previous'])

    def test_every_commit_gets_the_next_revision(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-1')
        self.commit(message='docs only')
        self.assertEqual(version.for_main()['version'], '157.0.8103.39-2')

    def test_a_new_pin_starts_again(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-4')
        self.commit(tag='158.0.8200.10')
        self.assertEqual(version.for_main()['version'], '158.0.8200.10-1')

    def test_a_revert_still_moves_forward(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-1')
        self.commit(tag='158.0.8200.10')
        self.git('tag', 'v158.0.8200.10-1')
        self.commit(tag='157.0.8103.39', message='revert')
        self.assertEqual(version.for_main()['version'], '157.0.8103.39-2')

    def test_an_older_commit_publishes_nothing(self):
        old = self.commit()
        self.commit()
        self.git('tag', 'v157.0.8103.39-1')
        answer = version.for_main(old)
        self.assertFalse(answer['publish'])
        self.assertIn('v157.0.8103.39-1', answer['reason'])

    def test_the_released_commit_itself_publishes_nothing(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-1')
        self.assertFalse(version.for_main()['publish'])

    def test_unreleased_commits_are_listed(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-1')
        self.commit(message='second')
        self.commit(message='third')
        answer = version.for_main()
        self.assertEqual(answer['version'], '157.0.8103.39-2')
        self.assertEqual([line.split(' ', 1)[1] for line in answer['unreleased']], ['second'])

    def test_other_tags_are_ignored(self):
        self.commit()
        self.git('tag', 'v157.0.8103.39-rc1')
        self.git('tag', 'something')
        self.assertEqual(version.for_main()['version'], '157.0.8103.39-1')

    def test_pull_requests(self):
        self.commit()
        self.assertEqual(version.for_pr('12'), '157.0.8103.39-pr12')
        with self.assertRaises(SystemExit):
            version.for_pr('12; rm')


if __name__ == '__main__':
    unittest.main()
