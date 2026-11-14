import json
import tempfile
import unittest
from pathlib import Path

from tests.scripts.common import REPO  # noqa: F401
import publish

PIN = {'tag': '157.0.8103.39', 'commit': '9'*40}


def assets(directory, sizes):
    for name, data in sizes.items():
        (directory/name).write_bytes(data)


class Manifest(unittest.TestCase):
    def test_lists_every_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            names = ['chromium-sdk-1-linux-amd64-lib.tar.zst', 'chromium-sdk-1-source-chromium-sdk.tar.zst']
            assets(tmp, {name: name.encode() for name in names})
            described = {names[0]: {'target': 'linux-amd64', 'part': 'lib'}, names[1]: {'part': 'source'}}
            manifest = publish.write_manifest(tmp, '1', 'c'*40, PIN, described)
            self.assertEqual([a['name'] for a in manifest['assets']], sorted(names))
            self.assertEqual(manifest['tag'], 'v1')
            sums = (tmp/'SHA256SUMS').read_text().splitlines()
            self.assertEqual(len(sums), 3)
            self.assertTrue(sums[-1].endswith('  manifest.json'))
            publish.check_local(tmp, manifest)
            (tmp/names[0]).write_bytes(b'changed')
            with self.assertRaises(SystemExit):
                publish.check_local(tmp, manifest)

    def test_undescribed_or_missing_assets_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            assets(tmp, {'chromium-sdk-1-x.tar.zst': b'x'})
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, PIN, {})
            with self.assertRaises(SystemExit):
                publish.write_manifest(tmp, '1', 'c'*40, PIN, {'chromium-sdk-1-x.tar.zst': {},
                                                               'chromium-sdk-1-y.tar.zst': {}})

    def test_two_jobs_may_describe_one_asset_the_same_way(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            entry = {'name': 'chromium-sdk-1-source-x.tar.zst', 'part': 'source', 'size': 3, 'dir': 'x'}
            (tmp/'assets-a.json').write_text(json.dumps([entry]))
            (tmp/'assets-b.json').write_text(json.dumps([dict(entry, size=3)]))
            described = publish.merge_descriptions([tmp/'assets-a.json', tmp/'assets-b.json'])
            self.assertEqual(described, {'chromium-sdk-1-source-x.tar.zst': {'part': 'source', 'dir': 'x'}})
            (tmp/'assets-c.json').write_text(json.dumps([dict(entry, dir='y')]))
            with self.assertRaises(SystemExit):
                publish.merge_descriptions([tmp/'assets-a.json', tmp/'assets-c.json'])

    def test_size_limit(self):
        self.assertEqual(publish.too_large({'a': publish.LIMIT-1, 'b': publish.LIMIT, 'c': publish.LIMIT+1}),
                         ['b', 'c'])

    def test_gather_refuses_two_different_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for job, data in (('one', b'a'), ('two', b'a')):
                (tmp/job).mkdir()
                assets(tmp/job, {'chromium-sdk-1-source-x.tar.zst': data})
                (tmp/job/'assets-sources.json').write_text('[]')
            publish.gather(tmp/'out', [tmp/'one', tmp/'two'])
            self.assertTrue((tmp/'out/chromium-sdk-1-source-x.tar.zst').exists())
            (tmp/'three').mkdir()
            assets(tmp/'three', {'chromium-sdk-1-source-x.tar.zst': b'b'})
            with self.assertRaises(SystemExit):
                publish.gather(tmp/'out2', [tmp/'one', tmp/'three'])


class FakeGitHub:
    """Records what release() asks of GitHub."""

    def __init__(self, tags=(), releases=()):
        self.tags = set(tags)
        self.existing = list(releases)
        self.calls = []
        self.uploaded = {}
        self.published = False

    def tag_exists(self, tag):
        return tag in self.tags

    def releases(self):
        return self.existing

    def api(self, method, path, body=None, expect=(200,)):
        self.calls.append((method, path))
        if method == 'POST' and path == '/releases':
            return 201, {'id': 7}
        if method == 'GET' and path.startswith('/releases/7/assets'):
            return 200, [{'id': i, 'name': name, 'size': len(data), 'digest': 'sha256:'+publish.hashlib.sha256(data).hexdigest()}
                         for i, (name, data) in enumerate(sorted(self.uploaded.items()))]
        if method == 'PATCH':
            self.published = True
            self.tags.add(body['tag_name'])
            return 200, {'html_url': 'https://example.invalid/release'}
        if method == 'GET' and path.startswith('/git/ref/tags/'):
            return 200, {'object': {'sha': 'c'*40}}
        return 204, None

    def upload(self, release, path):
        self.uploaded[path.name] = path.read_bytes()

    def asset_sha256(self, asset):
        return asset['digest'][len('sha256:'):]


class Release(unittest.TestCase):
    def prepared(self, tmp):
        names = ['chromium-sdk-1-linux-amd64-lib.tar.zst']
        assets(tmp, {name: b'lib' for name in names})
        publish.write_manifest(tmp, '1', 'c'*40, PIN, {names[0]: {'target': 'linux-amd64', 'part': 'lib'}})

    def test_publishes_after_uploading_and_verifying(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub()
            publish.release(tmp, github)
            self.assertTrue(github.published)
            self.assertEqual(sorted(github.uploaded), ['SHA256SUMS', 'chromium-sdk-1-linux-amd64-lib.tar.zst',
                                                       'manifest.json'])
            self.assertEqual(github.calls[-2][0], 'PATCH')

    def test_an_existing_tag_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub(tags=['v1'])
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            self.assertEqual(github.calls, [])

    def test_a_published_release_fails_and_a_draft_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub(releases=[{'id': 3, 'tag_name': 'v1', 'name': '1', 'draft': False}])
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            github = FakeGitHub(releases=[{'id': 3, 'tag_name': 'v1', 'name': '1', 'draft': True}])
            publish.release(tmp, github)
            self.assertEqual(github.calls[0], ('DELETE', '/releases/3'))
            self.assertTrue(github.published)

    def test_a_corrupted_upload_stays_a_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.prepared(tmp)
            github = FakeGitHub()
            original = github.upload
            github.upload = lambda release, path: (original(release, path),
                                                   github.uploaded.__setitem__(path.name, b'bad')
                                                   if path.name.endswith('lib.tar.zst') else None)
            with self.assertRaises(SystemExit):
                publish.release(tmp, github)
            self.assertFalse(github.published)


if __name__ == '__main__':
    unittest.main()
