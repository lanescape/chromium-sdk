"""Notices and corresponding source for an SDK version (specs/0004-licences.md).

Notices ship inside the library part and as their own part. Corresponding source is every component whose licence
asks for it, found from Chromium's own metadata, as built: the checkout's files, with the SDK's patch applied. The
Linux sysroots are Debian's packages, so their source is Debian's source packages at the versions in them.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import archive

REPO = Path(__file__).resolve().parent.parent
# Licences that ask for the source of what's distributed, in Chromium's README.chromium License fields.
COPYLEFT = re.compile(r'\b(L?GPL|MPL|EPL|CDDL|CPL|OSL)\b|lgpl|mpl-|gpl-', re.IGNORECASE)
# Never inputs to the build: Blink's web tests are most of its directory.
UNBUILT = {'web_tests', 'perf_tests', 'manual_tests', '.git'}
TARGET = '//chromium_sdk:chromium_sdk'

_DISCOVER = r'''
import argparse, json, sys
sys.path.insert(0, "tools/licenses")
import licenses
args = argparse.Namespace(extra_third_party_dirs=None, extra_allowed_dirs=None, exclude_dirs=None,
                          scan_root=".", gn_out_dir=sys.argv[1], gn_target=sys.argv[2], enable_warnings=True,
                          shipped_only=False, target_os=None)
# Diagnostics go to stderr, so stdout is only the answer.
real_stdout, sys.stdout = sys.stdout, sys.stderr
metadatas, had_errors = licenses._DiscoverMetadatas(args)
sys.stdout = real_stdout
print(json.dumps({"errors": bool(had_errors),
                  "components": [{"name": m["Name"], "license": m.get("License", ""), "dir": m.get("dir", ".")}
                                 for m in metadatas]}))
'''
# Rust crates: Chromium's metadata sits in third_party/rust/<crate>/<epoch>, and the source it builds is vendored.
VENDORED = re.compile(r'//third_party/rust/chromium_crates_io/vendor/([^/"]+)/')


def components(src, out):
    """Every third-party component the SDK's targets link or ship: name, licence and directory under src."""
    gn_out = os.path.relpath(out, src)
    text = subprocess.check_output([sys.executable, '-c', _DISCOVER, gn_out, TARGET], cwd=src, text=True)
    answer = json.loads(text)
    if answer['errors']:
        # A component whose metadata Chromium can't read is left out of its credits: never publish without it.
        raise SystemExit("Chromium's licence metadata has errors (above); no notices or source without them")
    found = []
    for entry in answer['components']:
        directory = os.path.relpath(os.path.join(src, entry['dir']), src) if entry['dir'] else '.'
        entry['dir'] = directory
        entry['copyleft'] = bool(COPYLEFT.search(entry['license'])) and directory != '.'
        found.append(entry)
    return found


def notices(src, out, destination):
    """Chromium's licence, the credits for everything the build links, this repository's licence, and the list of
    components with their licences."""
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(src)/'LICENSE', destination/'LICENSE.chromium')
    shutil.copy2(REPO/'LICENSE', destination/'LICENSE.chromium-sdk')
    subprocess.run([sys.executable, 'tools/licenses/licenses.py', 'credits', '--gn-target', TARGET,
                    '--gn-out-dir', os.path.relpath(out, src), str(destination/'CREDITS.html')],
                   cwd=src, check=True)
    listed = components(src, out)
    (destination/'components.json').write_text(json.dumps(listed, indent=2, sort_keys=True)+'\n')
    (destination/'README').write_text(
        'Notices for this Chromium SDK version.\n\n'
        'LICENSE.chromium      Chromium\'s licence.\n'
        'CREDITS.html          The licences of every third-party component the SDK\'s library and files link\n'
        '                      or ship.\n'
        'LICENSE.chromium-sdk  The licence of the scripts that built this version.\n'
        'components.json       Each component, its licence, and whether its corresponding source is published\n'
        '                      with this version (copyleft: true), as a source-<name> asset of the same release.\n')
    return listed


def toolchain_notice(src, destination):
    """The toolchain is LLVM's, under the Apache License 2.0 with LLVM Exceptions, which libc++ shares."""
    shutil.copy2(Path(src)/'third_party/libc++/src/LICENSE.TXT', destination/'LICENSE.TXT')


def source_dirs(src, directory):
    """The directories that hold a component's source as built: its own, and for a Rust crate, its vendored
    source, which its BUILD.gn names."""
    dirs = [directory]
    build = Path(src)/directory/'BUILD.gn'
    if directory.startswith('third_party/rust/') and build.is_file():
        for vendored in sorted(set(VENDORED.findall(build.read_text()))):
            path = f'third_party/rust/chromium_crates_io/vendor/{vendored}'
            if (Path(src)/path).is_dir() and path not in dirs:
                dirs.append(path)
    return dirs


def slug(directory):
    return re.sub(r'[^a-z0-9.]+', '-', directory.lower()).strip('-')


def source_archives(src, out, output, version):
    """Archives each copyleft component's directory, as built, beside the parts. Returns the assets written."""
    written = []
    for component in sorted(components(src, out), key=lambda c: c['dir']):
        if not component['copyleft']:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            for directory in source_dirs(src, component['dir']):
                shutil.copytree(Path(src)/directory, Path(tmp)/directory, symlinks=True,
                                ignore=lambda _, names: [n for n in names if n in UNBUILT])
            asset = Path(output)/f'chromium-sdk-{version}-source-{slug(component["dir"])}.tar.zst'
            archive.write(tmp, asset)
        written.append({'name': asset.name, 'part': 'source', 'component': component['name'],
                        'license': component['license'], 'dir': component['dir'], 'size': asset.stat().st_size})
    return written


# Debian's source packages for a sysroot.

def sysroot_packages(sysroot):
    """(source package, version) for every binary package in a Chromium sysroot."""
    found = set()
    for control in sorted(Path(sysroot).glob('debian/*/DEBIAN/control')):
        fields = parse_control(control.read_text(errors='replace'))
        version = fields['Version']
        source = fields.get('Source', fields['Package'])
        match = re.match(r'^(\S+)\s+\(([^)]+)\)$', source)
        if match:
            source, version = match.groups()
        found.add((source, version))
    return sorted(found)


def parse_control(text):
    fields = {}
    key = None
    for line in text.splitlines():
        if line[:1] in (' ', '\t') and key:
            fields[key] += '\n'+line.strip()
        elif ':' in line:
            key, value = line.split(':', 1)
            fields[key] = value.strip()
    return fields


def pool_path(source, version):
    prefix = source[:4] if source.startswith('lib') else source[0]
    upstream = version.split(':', 1)[-1]
    return f'pool/main/{prefix}/{source}/{source}_{upstream}.dsc'


def snapshot_base(package_list):
    first = Path(package_list).read_text().split()[0]
    return first[:first.index('/pool/')+1]


def fetch(url, destination, sha=None):
    """Downloads a file into the cache, whole or not at all: a download a stopped machine cut short never
    reaches its name."""
    if destination.exists() and (sha is None or sha256(destination) == sha):
        return
    partial = destination.with_name(destination.name+'.partial')
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=300) as stream, open(partial, 'wb') as out:
                shutil.copyfileobj(stream, out)
            break
        except OSError:
            if attempt == 4:
                raise
    if sha is not None and sha256(partial) != sha:
        raise SystemExit(f'{url} does not match its .dsc')
    os.replace(partial, destination)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def sysroot_sources(src, sysroots, cache, output, version, name):
    """Downloads the Debian source packages of every package in the sysroots, verified against their .dsc, and
    archives them with each package's copyright as one asset."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    packages = set()
    bases = set()
    for sysroot, package_list in sysroots:
        packages.update(sysroot_packages(Path(src)/sysroot))
        bases.add(snapshot_base(Path(src)/package_list))
    if len(bases) != 1:
        raise SystemExit(f'sysroots come from different snapshots: {bases}')
    base = bases.pop()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)/'debian'
        stage.mkdir()
        listed = []
        for source, version_ in sorted(packages):
            dsc_path = pool_path(source, version_)
            directory = stage/source
            directory.mkdir(exist_ok=True)
            dsc = cache/Path(dsc_path).name
            fetch(base+dsc_path, dsc)
            files = checksums(dsc.read_text(errors='replace'))
            shutil.copy2(dsc, directory/dsc.name)
            for file_name, sha in files:
                cached = cache/file_name
                fetch(base+str(Path(dsc_path).parent/file_name), cached, sha)
                shutil.copy2(cached, directory/file_name)
            listed.append({'source': source, 'version': version_, 'dsc': base+dsc_path})
        (Path(tmp)/'packages.json').write_text(json.dumps(listed, indent=2)+'\n')
        asset = Path(output)/f'chromium-sdk-{version}-source-{name}.tar.zst'
        # Debian's files are compressed already.
        archive.write(tmp, asset, level=3)
    return {'name': asset.name, 'part': 'source', 'component': name, 'license': 'Debian source packages',
            'size': asset.stat().st_size}


def checksums(dsc_text):
    fields = parse_control(dsc_text)
    entries = []
    for line in fields.get('Checksums-Sha256', '').splitlines():
        parts = line.split()
        if len(parts) == 3:
            entries.append((parts[2], parts[0]))
    if not entries:
        raise SystemExit('a .dsc without SHA-256 checksums')
    return entries


def sdk_source(output, version, commit='HEAD'):
    """This repository at the built commit: the settings, the patch and the scripts that built the version."""
    asset = Path(output)/f'chromium-sdk-{version}-source-chromium-sdk.tar.zst'
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp)/'repo.tar'
        subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', f'--prefix=chromium-sdk/', '-o', str(tar),
                        commit], check=True)
        subprocess.run(['tar', '-xf', str(tar), '-C', tmp], check=True)
        tar.unlink()
        archive.write(tmp, asset)
    return {'name': asset.name, 'part': 'source', 'component': 'chromium-sdk', 'license': 'MIT',
            'size': asset.stat().st_size}
