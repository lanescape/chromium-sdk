#!/usr/bin/env python3
"""Publishes an SDK version as a GitHub release (specs/0003-ci-cd.md, Publishing).

    publish.py gather <directory> <job output>...                  merges the jobs' assets into one directory
    publish.py manifest <directory> --version <v> --commit <sha> --repository <owner/name>
                                                                    writes manifest.json and SHA256SUMS
    publish.py release <directory>                                  publishes them and every asset they list

`manifest` checks every asset and fails, naming it, if any is 2 GiB or larger, GitHub's limit for a release asset.
`manifest` and `release` both fail if any part of any target in targets.json is missing, or if any was packaged
quickly, as CI's are, or if any corresponding source is missing: this repository's, the Linux sysroots' and that of
every component a target's notices mark as copyleft, each by the name `sdk.py sources` gives it
(specs/0004-licences.md).
The manifest and the release notes say where the source is that the release points to rather than copies:
Chromium's official source tarball for the pin, its checksums, the Chromium tag, and this repository's patch at
the release's tag and commit (specs/0004-licences.md). `release` checks Chromium's tarball and checksums are
published before it publishes anything, and refuses if the version's tag or a published release for it exists, replaces only a draft a failed run
left, uploads every asset to a new draft, verifies each one's SHA-256 against the manifest, and then publishes the
draft, which creates the tag last. It never changes or deletes a published release or a tag.

It reads the repository from GITHUB_REPOSITORY and a token from GITHUB_TOKEN, and never prints the token or any
URL it is given for an upload or download. When a request fails, it prints only the method, the URL's path, and
GitHub's status or the kind of failure: never what GitHub answered, which can hold an upload's or a download's
address. The token goes only to GitHub's API and uploads: an asset's download sends it to GitHub, never on to where
GitHub redirects.
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import archive
import licences

REPO = Path(__file__).resolve().parent.parent

LIMIT = 2*1024**3
API = 'https://api.github.com'
UPLOADS = 'https://uploads.github.com'
NAME = re.compile(r'^libchromium-[0-9A-Za-z.\-]+\.tar\.zst$')
MANIFEST = 'manifest.json'
# The list of a target's components, in its notices part (specs/0004-licences.md, Notices).
COMPONENTS = 'notices/components.json'
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
                      'url': licences.patch_url(tag, repository),
                      'commit_url': licences.patch_url(commit, repository)}}


def targets():
    return json.loads((Path(__file__).resolve().parent.parent/'targets.json').read_text())


def check_parts(described, targets):
    """A release holds every part of every target in targets.json (package.parts_of()), each packaged for a
    release: none of CI's quick ones. `described` maps an asset's name to what its job said about it."""
    import package
    quick = sorted(name for name, description in described.items() if description.get('quick'))
    if quick:
        raise SystemExit(f'packaged quickly, for CI\'s tests alone, and never published: {", ".join(quick)}')
    have = {(description.get('target'), description.get('part')) for description in described.values()}
    missing = [f'{target} {part}' for target, entry in sorted(targets.items()) for part in package.parts_of(entry)
               if (target, part) not in have]
    if missing:
        raise SystemExit(f'parts missing: {", ".join(missing)}')


def scanned(path, wanted=(), digested=()):
    """An archive's members, wanted files and digests (archive.scan()), or a failure naming it if it isn't whole."""
    try:
        return archive.scan(path, wanted, digested)
    except (OSError, RuntimeError, tarfile.TarError):
        raise SystemExit(f'{Path(path).name} is not a whole .tar.zst archive') from None


def components_of(path):
    """The components a target's notices part lists, from the part's archive: never none, since every target links
    some, and each with its name, its directory and whether its source is published."""
    data = scanned(path, (COMPONENTS,))[1].get(COMPONENTS)
    if data is None:
        raise SystemExit(f'{Path(path).name} has no {COMPONENTS}')
    try:
        listed = json.loads(data)
    except ValueError:
        listed = None
    if not isinstance(listed, list) or not listed or not all(
            isinstance(entry, dict) and isinstance(entry.get('copyleft'), bool)
            and all(isinstance(entry.get(key), str) and entry[key].strip() for key in ('name', 'dir'))
            and (not entry['copyleft'] or (isinstance(entry.get('source_dirs'), list) and entry['source_dirs']
                                           and entry['source_dirs'][0] == entry['dir']
                                           and all(isinstance(d, str) and d.strip() for d in entry['source_dirs'])
                                           and isinstance(entry.get('source_files'), int)
                                           and not isinstance(entry['source_files'], bool)
                                           and entry['source_files'] > 0
                                           and isinstance(entry.get('source_listing'), str)
                                           and re.fullmatch(r'[0-9a-f]{64}', entry['source_listing'])))
            for entry in listed):
        raise SystemExit(f'{Path(path).name}\'s {COMPONENTS} is not a list of components')
    return listed


def source_asset(version, name):
    """The name `sdk.py sources` gives a source asset: a component's by the slug of its directory."""
    return f'libchromium-{version}-source-{name}.tar.zst'


PACKAGES = 'packages.json'


def tracked(commit):
    """Every file this repository tracks at a commit, which its source asset must hold, as the kind and size, or
    link target's length, its member must have, by its name in the asset."""
    result = subprocess.run(['git', '-C', str(REPO), 'ls-tree', '-r', '-l', '-z', commit],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise SystemExit(f'git can\'t list the files at {commit}, which this repository\'s source must hold')
    files = {}
    for line in result.stdout.decode().split('\0'):
        if not line:
            continue
        fields, name = line.split('\t', 1)
        mode, kind, _, size = fields.split()
        if kind == 'blob':
            files[f'libchromium/{name}'] = ('symlink' if mode == '120000' else 'file', int(size))
    return files


def sysroot_packages(directory, described, targets):
    """(source package, version) of every package in every target's sysroot, from the sysroot parts, which ship each
    sysroot whole, with its packages' control files."""
    found = set()
    for target, entry in sorted(targets.items()):
        if not entry.get('sysroot'):
            continue
        part = [name for name, description in described.items()
                if description.get('target') == target and description.get('part') == 'sysroot']
        if len(part) != 1:
            raise SystemExit(f'{target} has {len(part)} sysroot parts, not 1')
        control = re.compile(r'^sysroot/debian/[^/]+/DEBIAN/control$')
        _, files, _ = scanned(Path(directory)/part[0], control.match)
        found.update(licences.packages_of([text.decode(errors='replace') for _, text in sorted(files.items())],
                                          f'{target}\'s sysroot'))
    return found


def holds(path, need=(), files=None, listings=None, debian=None):
    """Why a source asset doesn't hold what it must, or None. It must be a whole archive (archive.scan()), with a
    regular file under each directory in `need`, ending in /; with, for each name in `files`, a member of that kind
    and size, or link target's length; and for each tuple of directories in `listings`, exactly the entries below
    them that a component's notices list, by the digest of their listing (archive.listing_digest()) and how many
    files they are. If `debian` is the set of packages in the sysroots, as (source, version), its packages.json must
    list every one, each with its own whole .dsc and every file that names, matching its SHA-256 there, under
    debian/<source>/, as `sdk.py sources` lays them out."""
    members, contents, sums = scanned(
        path, (lambda name: name == PACKAGES or name.endswith('.dsc')) if debian is not None else (),
        (lambda name: name.startswith('debian/')) if debian is not None else ())
    by_name = {member.name: member for member in members}
    regular = [member.name for member in members if member.kind == 'file']
    absent = [wanted for wanted in sorted(need) if not any(name.startswith(wanted) for name in regular)]
    for name, (kind, size) in sorted((files or {}).items()):
        member = by_name.get(name)
        if member is None or member.kind != kind or (
                member.size if kind == 'file' else len(member.link.encode())) != size:
            absent.append(name)
    if absent:
        return f'it lacks or has changed {", ".join(absent[:20])}{" and more" if len(absent) > 20 else ""}'
    for dirs, (digest, count) in sorted((listings or {}).items()):
        below = [member for member in members if member.name.startswith(dirs)]
        if archive.listing_digest(below) != digest:
            found = sum(1 for member in below if member.kind == 'file')
            return f'its files in {", ".join(dirs)} aren\'t those its notices list, {found} of {count} files'
    if debian is not None:
        try:
            packages = json.loads(contents.get(PACKAGES) or b'')
        except ValueError:
            packages = None
        if not isinstance(packages, list) or not packages or not all(
                isinstance(entry, dict) and all(isinstance(entry.get(key), str) and entry[key].strip()
                                                for key in ('source', 'version', 'dsc'))
                and '/' not in entry['source'] for entry in packages):
            return f'its {PACKAGES} lists no packages'
        unlisted = sorted(debian - {(entry['source'], entry['version']) for entry in packages})
        if unlisted:
            return f'its {PACKAGES} leaves out {", ".join(f"{source} {version}" for source, version in unlisted)}'
        unsourced = []
        for entry in packages:
            directory = f'debian/{entry["source"]}/'
            dsc = directory+entry['dsc'].rsplit('/', 1)[-1]
            text = contents.get(dsc, b'').decode(errors='replace')
            try:
                named = licences.checksums(text) if licences.complete_dsc(text) else None
            except SystemExit:
                named = None
            if named is None or any(sums.get(directory+name) != sha for name, sha in named):
                unsourced.append(f'{entry["source"]} {entry["version"]}')
        if unsourced:
            return f'it doesn\'t hold the whole source of {", ".join(sorted(unsourced))}'
    return None


def check_sources(directory, version, commit, described, targets):
    """A release holds the corresponding source of everything it publishes whose licence asks for it, each as the
    asset `sdk.py sources` names: this repository's, the Linux sysroots' Debian sources if any target has a sysroot,
    and for every target, the source of each component its notices mark as copyleft, archived by the component's
    directory. Each must be a whole archive that holds what it's for (holds()): this repository's every file it
    tracks at `commit`, the sysroots' every package, and a component's every file its notices list. `described`
    maps an asset's name to what its job said about it; every target's notices and sysroot parts and every source
    asset are in `directory`."""
    def archived(name, directory_=None):
        description = described.get(name) or {}
        return description.get('part') == 'source' and (
            directory_ is None or directory_ in (description.get('dir') or '').split(', '))

    # Each source asset there must be, and what it must hold.
    needed = {source_asset(version, 'libchromium'): {'files': tracked(commit)}}
    debian = source_asset(version, licences.SYSROOT_SOURCE)
    if any(entry.get('sysroot') for entry in targets.values()):
        needed[debian] = {'files': {}}
    missing = [name for name in needed if not archived(name)]
    for target in sorted(targets):
        notices = [name for name, description in described.items()
                   if description.get('target') == target and description.get('part') == 'notices']
        if len(notices) != 1:
            raise SystemExit(f'{target} has {len(notices)} notices parts, not 1')
        for component in components_of(Path(directory)/notices[0]):
            if not component['copyleft']:
                continue
            asset = source_asset(version, licences.slug(component['dir']))
            if archived(asset, component['dir']):
                # The component's directory and every other its notices name, such as a crate's vendored source.
                dirs = tuple(directory_.rstrip('/')+'/' for directory_ in component['source_dirs'])
                check = needed.setdefault(asset, {'need': set(), 'listings': {}})
                check['need'].update(dirs)
                listing = (component['source_listing'], component['source_files'])
                # One asset serves every target that links the component, so every target must list the same.
                if check['listings'].setdefault(dirs, listing) != listing:
                    raise SystemExit(f'the targets\' notices list different files for {asset}')
            else:
                missing.append(f'{asset}, for {target}\'s {component["name"]} ({component["dir"]})')
    if missing:
        raise SystemExit(f'corresponding source missing: {", ".join(sorted(set(missing)))}')
    packages = sysroot_packages(directory, described, targets) if debian in needed else None
    broken = [f'{name}: {why}' for name, check in sorted(needed.items())
              if (why := holds(Path(directory)/name, debian=packages if name == debian else None, **check))]
    if broken:
        raise SystemExit(f'corresponding source not whole: {"; ".join(broken)}')


def check_release(directory, version, commit, described, targets):
    """Every part of every target, none of them quick, and every corresponding source."""
    check_parts(described, targets)
    check_sources(directory, version, commit, described, targets)


def write_manifest(directory, version, commit, pin, described, repository):
    """manifest.json lists every asset in the directory with its size and SHA-256, and where the source is that the
    release points to; SHA256SUMS lists the assets and the manifest. `described` maps an asset's name to what the
    jobs that made it said about it."""
    directory = Path(directory)
    assets = sorted(path for path in directory.iterdir() if path.is_file() and NAME.match(path.name))
    quick = sorted(name for name, description in described.items() if description.get('quick'))
    if quick:
        raise SystemExit(f'packaged quickly, for CI\'s tests alone, and never published: {", ".join(quick)}')
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
        headers.update({'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                        'User-Agent': 'libchromium-publish'})
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        # The token goes where it's sent, to GitHub, and never on with a redirect.
        request.add_unredirected_header('Authorization', f'Bearer {self.token}')
        # The path only: never a query string, which can carry a signature.
        path = urllib.parse.urlsplit(url).path
        try:
            try:
                with urllib.request.urlopen(request, timeout=600) as response:
                    status, text = response.status, response.read()
            except urllib.error.HTTPError as error:
                with error:
                    status, text = error.code, error.read()
        except (OSError, http.client.HTTPException, ValueError) as error:
            # A dropped connection or a timeout, even while reading a refusal, or a redirect to an address that
            # isn't one: its kind only, since its text can hold where it was going.
            raise SystemExit(f'{method} {path} failed: {type(error).__name__}') from None
        if status not in expect:
            # Never what GitHub answered, which can hold an upload's or a download's address.
            raise SystemExit(f'GitHub answered {status} to {method} {path}')
        try:
            return status, (json.loads(text) if text else None)
        except ValueError:
            # An answer cut short, as a dropped connection can leave one: like the drop itself, and never its text.
            raise SystemExit(f'{method} {path} failed: GitHub\'s answer is not whole') from None

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
        """Uploads an asset, trying 3 times whether GitHub refused it or the connection dropped."""
        name = urllib.parse.quote(path.name)
        url = f'{UPLOADS}/repos/{self.repository}/releases/{release}/assets?name={name}'
        for attempt in range(3):
            with open(path, 'rb') as stream:
                try:
                    uploaded = self.request('POST', url, stream, {'Content-Type': 'application/octet-stream',
                                                                  'Content-Length': str(path.stat().st_size)},
                                            expect=(201,))[1]
                    if not isinstance(uploaded, dict):
                        # A connection closed after GitHub's headers leaves no answer.
                        raise SystemExit(f'uploading {path.name} failed: GitHub\'s answer is not whole')
                    return uploaded
                except SystemExit as error:
                    if attempt == 2:
                        raise
                    print(f'{error}; trying {path.name} again', flush=True)
            time.sleep(10)
            # A failed upload can leave a partial asset under the name; remove it before trying again. If that
            # fails too, the next try finds the name taken, and fails in its turn.
            try:
                listed = self.api('GET', f'/releases/{release}/assets?per_page=100')[1]
                if not isinstance(listed, list):
                    raise SystemExit(f'listing the draft\'s assets failed: GitHub\'s answer is not whole')
                for asset in listed:
                    if asset['name'] == path.name:
                        self.api('DELETE', f'/releases/assets/{asset["id"]}', expect=(204,))
            except SystemExit as error:
                print(f'{error}; trying {path.name} again all the same', flush=True)
        return None

    def asset_sha256(self, asset):
        """The asset's SHA-256 as GitHub reports it, or as downloaded when it doesn't."""
        digest = asset.get('digest') or ''
        if digest.startswith('sha256:'):
            return digest[len('sha256:'):]
        path = f'/repos/{self.repository}/releases/assets/{asset["id"]}'
        request = urllib.request.Request(f'{API}{path}', headers={'Accept': 'application/octet-stream',
                                                                  'User-Agent': 'libchromium-publish'})
        # GitHub answers with a redirect to where the asset is, whose address is signed: the token goes to GitHub
        # alone, never on with the redirect.
        request.add_unredirected_header('Authorization', f'Bearer {self.token}')
        hasher = hashlib.sha256()
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                while block := response.read(1 << 20):
                    hasher.update(block)
        except urllib.error.HTTPError as error:
            error.close()
            raise SystemExit(f'GitHub answered {error.code} to GET {path}') from None
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise SystemExit(f'GET {path} failed: {type(error).__name__}') from None
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
            error.close()
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
    check_release(directory, manifest['version'], manifest['commit'],
                  {entry['name']: entry for entry in manifest['assets']}, targets())
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
        raise SystemExit(f'{tag} names another commit than {manifest["commit"]}')
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
        check_release(args.directory, args.version, args.commit, described, targets())
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
