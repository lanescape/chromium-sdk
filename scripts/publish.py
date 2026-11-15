#!/usr/bin/env python3
"""Publishes an SDK version as a GitHub release (specs/0003-ci-cd.md, Publishing).

    publish.py gather <directory> <job output>...                  merges the jobs' assets into one directory
    publish.py manifest <directory> --version <v> --commit <sha> --repository <owner/name>
                                                                    writes manifest.json and SHA256SUMS
    publish.py release <directory>                                  publishes them and every asset they list

`manifest` checks every asset and fails, naming it, if any is 2 GiB or larger, GitHub's limit for a release asset.
The manifest and the release notes say where the source is that the release points to rather than copies:
Chromium's official source tarball for the pin, its checksums, the Chromium tag, and this repository's patch at
the release's tag and commit (specs/0004-licences.md). `release` checks Chromium's tarball and checksums are
published before it publishes anything, and refuses if the version's tag or a published release for it exists, replaces only a draft a failed run
left, uploads every asset to a new draft, verifies each one's SHA-256 against the manifest, and then publishes the
draft, which creates the tag last. It never changes or deletes a published release or a tag.

It reads the repository from GITHUB_REPOSITORY and a token from GITHUB_TOKEN, and never prints the token or any
URL it is given for an upload or download.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import licences

LIMIT = 2*1024**3
API = 'https://api.github.com'
UPLOADS = 'https://uploads.github.com'
NAME = re.compile(r'^libchromium-[0-9A-Za-z.\-]+\.tar\.zst$')
PATCHES = 'chromium/patches'
MANIFEST = 'manifest.json'
SUMS = 'SHA256SUMS'


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def too_large(sizes):
    """The names of assets GitHub would refuse."""
    return sorted(name for name, size in sizes.items() if size >= LIMIT)


def gather(destination, sources):
    """Copies every job's assets and asset lists into one directory. Two jobs may make the same asset, such as a
    component's source that two targets link, but only with the same bytes."""
    import shutil
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    seen = {}
    for source in sources:
        for path in sorted(Path(source).iterdir()):
            if not path.is_file():
                continue
            if NAME.match(path.name):
                digest = sha256(path)
                if path.name in seen and seen[path.name] != digest:
                    raise SystemExit(f'two jobs made {path.name} with different contents')
                seen[path.name] = digest
                if not (destination/path.name).exists():
                    try:
                        os.link(path, destination/path.name)
                    except OSError:
                        shutil.copy2(path, destination/path.name)
            elif path.name.startswith('assets-') and path.name.endswith('.json'):
                shutil.copy2(path, destination/f'assets-{Path(source).name}-{path.name[len("assets-"):]}')
    return seen


def merge_descriptions(listings):
    """What the jobs said about each asset, by name. Two jobs may describe one asset, but only the same way."""
    described = {}
    for listing in listings:
        for entry in json.loads(Path(listing).read_text()):
            name = entry['name']
            description = {key: value for key, value in entry.items() if key not in ('name', 'size')}
            if name in described and described[name] != description:
                raise SystemExit(f'two jobs described {name} differently')
            described[name] = description
    return described


def source(version, commit, pin, repository):
    """Where the source is that the release points to rather than copies: Chromium's at the pin, as its official
    tarball with its checksums file, and this repository's patch at the release's tag and commit, which the
    source-libchromium asset also holds with the scripts."""
    tag = f'v{version}'
    return {'chromium': licences.chromium_source(pin),
            'patch': {'tag': tag, 'commit': commit,
                      'url': f'https://github.com/{repository}/tree/{tag}/{PATCHES}',
                      'commit_url': f'https://github.com/{repository}/tree/{commit}/{PATCHES}'}}


def write_manifest(directory, version, commit, pin, described, repository):
    """manifest.json lists every asset in the directory with its size and SHA-256, and where the source is that the
    release points to; SHA256SUMS lists the assets and the manifest. `described` maps an asset's name to what the
    jobs that made it said about it."""
    directory = Path(directory)
    assets = sorted(path for path in directory.iterdir() if path.is_file() and NAME.match(path.name))
    undescribed = sorted(path.name for path in assets if path.name not in described)
    if undescribed:
        raise SystemExit(f'assets no job described: {", ".join(undescribed)}')
    missing = sorted(set(described) - {path.name for path in assets})
    if missing:
        raise SystemExit(f'described assets that are missing: {", ".join(missing)}')
    big = too_large({path.name: path.stat().st_size for path in assets})
    if big:
        raise SystemExit(f'2 GiB or larger, too large for a release asset: {", ".join(big)}')
    listed = []
    for path in assets:
        entry = dict(described[path.name])
        entry.update(name=path.name, size=path.stat().st_size, sha256=sha256(path))
        listed.append(entry)
    manifest = {'schema': 1, 'version': version, 'tag': f'v{version}', 'commit': commit, 'chromium': pin,
                'source': source(version, commit, pin, repository), 'assets': listed}
    (directory/MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
    lines = [f'{entry["sha256"]}  {entry["name"]}' for entry in listed]
    lines.append(f'{sha256(directory/MANIFEST)}  {MANIFEST}')
    (directory/SUMS).write_text('\n'.join(lines)+'\n')
    return manifest


def check_local(directory, manifest):
    """Every listed asset is here, unchanged, and nothing else is."""
    directory = Path(directory)
    names = {entry['name'] for entry in manifest['assets']}
    extra = sorted(path.name for path in directory.iterdir() if path.is_file() and NAME.match(path.name)
                   and path.name not in names)
    if extra:
        raise SystemExit(f'assets the manifest does not list: {", ".join(extra)}')
    for entry in manifest['assets']:
        path = directory/entry['name']
        if not path.is_file() or path.stat().st_size != entry['size'] or sha256(path) != entry['sha256']:
            raise SystemExit(f'{entry["name"]} does not match the manifest')


class GitHub:
    def __init__(self, repository, token):
        self.repository = repository
        self.token = token

    def request(self, method, url, body=None, headers=None, expect=(200,)):
        headers = dict(headers or {})
        headers.update({'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'libchromium-publish'})
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                status, text = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, text = error.code, error.read()
        if status not in expect:
            # The path only: never a query string, which can carry a signature.
            path = urllib.parse.urlsplit(url).path
            raise SystemExit(f'GitHub answered {status} to {method} {path}: {text[:300]!r}')
        return status, (json.loads(text) if text else None)

    def api(self, method, path, body=None, expect=(200,)):
        return self.request(method, f'{API}/repos/{self.repository}{path}', body, expect=expect)

    def tag_exists(self, tag):
        status, _ = self.api('GET', f'/git/ref/tags/{urllib.parse.quote(tag)}', expect=(200, 404))
        return status == 200

    def releases(self):
        page, found = 1, []
        while True:
            _, batch = self.api('GET', f'/releases?per_page=100&page={page}')
            found += batch
            if len(batch) < 100:
                return found
            page += 1

    def upload(self, release, path):
        name = urllib.parse.quote(path.name)
        url = f'{UPLOADS}/repos/{self.repository}/releases/{release}/assets?name={name}'
        for attempt in range(3):
            with open(path, 'rb') as stream:
                try:
                    return self.request('POST', url, stream, {'Content-Type': 'application/octet-stream',
                                                              'Content-Length': str(path.stat().st_size)},
                                        expect=(201,))[1]
                except SystemExit:
                    if attempt == 2:
                        raise
            # A failed upload can leave a partial asset under the name; remove it before trying again.
            for asset in self.api('GET', f'/releases/{release}/assets?per_page=100')[1]:
                if asset['name'] == path.name:
                    self.api('DELETE', f'/releases/assets/{asset["id"]}', expect=(204,))
            time.sleep(10)
        return None

    def asset_sha256(self, asset):
        """The asset's SHA-256 as GitHub reports it, or as downloaded when it doesn't."""
        digest = asset.get('digest') or ''
        if digest.startswith('sha256:'):
            return digest[len('sha256:'):]
        request = urllib.request.Request(
            f'{API}/repos/{self.repository}/releases/assets/{asset["id"]}',
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/octet-stream',
                     'User-Agent': 'libchromium-publish'})
        hasher = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=600) as response:
            while block := response.read(1 << 20):
                hasher.update(block)
        return hasher.hexdigest()


def body(manifest):
    targets = sorted({entry['target'] for entry in manifest['assets'] if entry.get('target')})
    chromium, patch = manifest['source']['chromium'], manifest['source']['patch']
    lines = [f'Chromium {manifest["chromium"]["tag"]} ({manifest["chromium"]["commit"]}), built by '
             f'{manifest["commit"]}.', '',
             'Each target is a set of parts, which extract into one directory: `lib` (the library, its runtime files, '
             '`sdk.json` and notices), `headers`, `toolchain`, `notices`, and on Linux `sysroot` and `symbols`. '
             '`manifest.json` lists every asset with its SHA-256; `SHA256SUMS` checks them with `sha256sum -c`.', '',
             f'Targets: {", ".join(targets)}.', '',
             '## Source', '',
             'The `source-*` assets are the corresponding source of every component whose licence asks for it, as '
             'built, and `source-libchromium` is this repository at the commit that built this version: the '
             'settings, the patch and the scripts. The rest of the source is pointed to, and `manifest.json` lists '
             'the same under `source`:', '',
             f'- Chromium {chromium["tag"]} (`{chromium["commit"]}`): Chromium\'s official source tarball, '
             f'{chromium["tarball"]}, and its checksums, {chromium["hashes"]}.',
             f'- The patch this version applies to it: {patch["url"]} (at `{patch["commit"]}`).', '',
             'Versions are not semver and promise no compatibility with each other: pin an exact version.']
    return '\n'.join(lines)


def is_public(url):
    """Whether a public URL answers, without credentials: GitHub's token never goes anywhere but GitHub."""
    request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'libchromium-publish'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status == 200
        except urllib.error.HTTPError as error:
            if error.code < 500:
                return False
        except OSError:
            pass
        if attempt < 2:
            time.sleep(10)
    return False


def release(directory, github):
    directory = Path(directory)
    manifest = json.loads((directory/MANIFEST).read_text())
    check_local(directory, manifest)
    # The release points to Chromium's source rather than copying it, so it publishes only once that is there.
    chromium = manifest['source']['chromium']
    for url in (chromium['tarball'], chromium['hashes']):
        if not is_public(url):
            raise SystemExit(f'{url} is not published, and the release points to it: rerun once it is')
    tag = manifest['tag']
    if github.tag_exists(tag):
        raise SystemExit(f'{tag} exists already: a version is published once')
    for existing in github.releases():
        if existing['tag_name'] == tag or existing['name'] == manifest['version']:
            if not existing['draft']:
                raise SystemExit(f'a published release for {manifest["version"]} exists already')
            print(f'removing a draft a failed run left for {manifest["version"]}', flush=True)
            github.api('DELETE', f'/releases/{existing["id"]}', expect=(204,))
    _, draft = github.api('POST', '/releases', {
        'tag_name': tag, 'target_commitish': manifest['commit'], 'name': manifest['version'],
        'body': body(manifest), 'draft': True, 'prerelease': False}, expect=(201,))
    files = [entry['name'] for entry in manifest['assets']] + [MANIFEST, SUMS]
    for name in files:
        print(f'uploading {name}', flush=True)
        github.upload(draft['id'], directory/name)
    expected = {entry['name']: entry for entry in manifest['assets']}
    expected[MANIFEST] = {'size': (directory/MANIFEST).stat().st_size, 'sha256': sha256(directory/MANIFEST)}
    expected[SUMS] = {'size': (directory/SUMS).stat().st_size, 'sha256': sha256(directory/SUMS)}
    uploaded = []
    page = 1
    while True:
        _, batch = github.api('GET', f'/releases/{draft["id"]}/assets?per_page=100&page={page}')
        uploaded += batch
        if len(batch) < 100:
            break
        page += 1
    if sorted(asset['name'] for asset in uploaded) != sorted(expected):
        raise SystemExit('the draft\'s assets are not the manifest\'s; it stays a draft')
    for asset in uploaded:
        want = expected[asset['name']]
        if asset['size'] != want['size'] or github.asset_sha256(asset) != want['sha256']:
            raise SystemExit(f'{asset["name"]} does not match the manifest after upload; the release stays a draft')
    if github.tag_exists(tag):
        raise SystemExit(f'{tag} appeared while uploading; the release stays a draft')
    _, published = github.api('PATCH', f'/releases/{draft["id"]}', {
        'draft': False, 'tag_name': tag, 'target_commitish': manifest['commit'], 'make_latest': 'true'})
    _, ref = github.api('GET', f'/git/ref/tags/{urllib.parse.quote(tag)}')
    if ref['object']['sha'] != manifest['commit']:
        raise SystemExit(f'{tag} names {ref["object"]["sha"]}, not {manifest["commit"]}')
    print(f'published {published["html_url"]}', flush=True)
    return published


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('manifest')
    make.add_argument('directory', type=Path)
    make.add_argument('--version', required=True)
    make.add_argument('--commit', required=True)
    make.add_argument('--repository', required=True, help='owner/name, where the patch is published')
    merge = sub.add_parser('gather')
    merge.add_argument('directory', type=Path)
    merge.add_argument('sources', nargs='+', type=Path)
    publish = sub.add_parser('release')
    publish.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'gather':
        gather(args.directory, args.sources)
        return 0
    if args.command == 'manifest':
        described = merge_descriptions(sorted(args.directory.glob('assets-*.json')))
        pin = json.loads((Path(__file__).resolve().parent.parent/'chromium/pin.json').read_text())
        write_manifest(args.directory, args.version, args.commit, pin, described, args.repository)
        print((args.directory/SUMS).read_text(), end='')
        return 0
    repository = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GITHUB_TOKEN']
    release(args.directory, GitHub(repository, token))
    return 0


if __name__ == '__main__':
    sys.exit(main())
