#!/usr/bin/env python3
"""Builds the SDK for one target: sdk.py <step> <target> [options] (specs/0002-build.md).

Steps, in order: checkout, packages, mac-sdk (macOS only), patch, configure, build, package, and for a release, sources. Each is safe to run again on a
kept disk, such as one restored or resumed by the runner service: it does nothing when its work is done, and a
build carries on from what ninja finds. The checkout is at $CHROMIUM_SDK_CHECKOUT, or ~/chromium-sdk/checkout.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEPOT_TOOLS_URL = 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'
CHROMIUM_URL = 'https://chromium.googlesource.com/chromium/src.git'
HERMETIC_XCODE = 'build/mac_files/xcode_binaries'
APPLE_SDK_IN_XCODE = 'Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
GN_DIR = 'chromium_sdk'


def checkout_root():
    return Path(os.environ.get('CHROMIUM_SDK_CHECKOUT', Path.home()/'chromium-sdk/checkout')).resolve()


def depot_tools():
    return Path(os.environ.get('DEPOT_TOOLS', checkout_root().parent/'depot_tools'))


def env():
    environment = dict(os.environ)
    environment['PATH'] = os.pathsep.join((str(depot_tools()), environment['PATH']))
    environment.update(DEPOT_TOOLS_UPDATE='0', DEPOT_TOOLS_METRICS='0', GCLIENT_SUPPRESS_GIT_VERSION_WARNING='1')
    return environment


def run(*command, cwd=None, check=True, capture=False, quiet=False):
    if not quiet:
        print('+', ' '.join(str(part) for part in command), flush=True)
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=env(), check=check, text=True,
                            stdout=subprocess.PIPE if capture or quiet else None,
                            stderr=subprocess.DEVNULL if quiet else None)
    return result.stdout if capture else result.returncode


def summary(line):
    print(line, flush=True)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a') as out:
            out.write(f'- {line}\n')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def load(name):
    return json.loads((REPO/name).read_text())


def pin():
    return load('chromium/pin.json')


def targets():
    return load('targets.json')


def target_entry(target):
    entries = targets()
    if target not in entries:
        raise SystemExit(f'unknown target {target}; targets.json has {", ".join(entries)}')
    return entries[target]


def out_dir(target):
    return checkout_root()/'src/out'/target


def gclient_digest():
    return hashlib.sha256((REPO/'chromium/gclient').read_bytes()).hexdigest()[:16]


# checkout

def checkout_record():
    """What the checkout was last synced to: the pin and .gclient, written when a sync finishes and removed before
    anything changes, so it only ever describes a checkout that is whole."""
    return checkout_root()/'.chromium-sdk-checkout.json'


def wanted_checkout():
    return {'pin': pin(), 'gclient': gclient_digest()}


def step_checkout(target, args):
    root = checkout_root()
    root.mkdir(parents=True, exist_ok=True)
    tools = depot_tools()
    if not (tools/'gclient').exists():
        # Into a new directory, then renamed, so a clone a stopped machine cut short is never taken for one.
        partial = tools.with_name(tools.name+'.partial')
        shutil.rmtree(partial, ignore_errors=True)
        run('git', 'clone', '--depth', '1', DEPOT_TOOLS_URL, partial)
        shutil.rmtree(tools, ignore_errors=True)
        partial.rename(tools)
    wanted = wanted_checkout()
    commit = wanted['pin']['commit']
    src = root/'src'
    head = (run('git', '-C', src, 'rev-parse', 'HEAD', capture=True, check=False, quiet=True) or '').strip() \
        if (src/'.git').exists() else ''
    record_path = checkout_record()
    gclient = (REPO/'chromium/gclient').read_text()
    on_disk = (root/'.gclient').read_text() if (root/'.gclient').exists() else None
    try:
        recorded = json.loads(record_path.read_text()) if record_path.exists() else None
    except ValueError:
        recorded = None  # Never whole: treat it as unfinished.
    if recorded == wanted and head == commit and on_disk == gclient:
        print('checkout: already at the pin, with this .gclient', flush=True)
        return
    record_path.unlink(missing_ok=True)
    (root/'.gclient').write_text(gclient)
    if not (src/'.git').exists():
        run('git', 'init', '-q', src)
    if run('git', '-C', src, 'remote', 'get-url', 'origin', check=False, quiet=True) != 0:
        run('git', '-C', src, 'remote', 'add', 'origin', CHROMIUM_URL)
    # The tag must name the pinned commit, whatever the checkout holds already.
    run('git', '-C', src, 'fetch', '--depth=1', 'origin', f'refs/tags/{wanted["pin"]["tag"]}')
    fetched = run('git', '-C', src, 'rev-parse', 'FETCH_HEAD', capture=True).strip()
    if fetched != commit:
        raise SystemExit(f'tag {wanted["pin"]["tag"]} is {fetched}, not the pinned {commit}')
    if head != commit:
        # Patched files would stop the checkout; they're the pin's own files with the SDK's patch applied.
        unpatch(src)
        run('git', '-C', src, 'checkout', '-q', '-f', '--detach', commit)
    # --reset discards what a patch left in a dependency's files when the pin moved; at the same pin there's
    # nothing to discard, and a patched file must keep its time.
    reset = ['--reset'] if head != commit else []
    run('gclient', 'sync', '--nohooks', '--no-history', '--force', *reset, '-r', f'src@{commit}', cwd=root)
    run('gclient', 'runhooks', cwd=root)
    partial = record_path.with_name(record_path.name+'.partial')
    partial.write_text(json.dumps(wanted)+'\n')
    os.replace(partial, record_path)


# packages

def step_packages(target, args):
    """Chromium's Linux build dependencies, from its own list. Not stamped: they live on the machine, which may be
    new, while the checkout lives on a kept disk."""
    if platform.system() != 'Linux':
        return
    script = checkout_root()/'src/build/install-build-deps.sh'
    if run('sudo', '-n', 'true', check=False, quiet=True) != 0:
        # Never wait for a password. Libraries come from Chromium's sysroot, so few of the list are used, and a
        # missing tool fails its build step by name.
        run(script, '--quick-check', check=False)
        return
    run('sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', script, '--no-prompt', '--no-chromeos-fonts')


# mac-sdk

def step_mac_sdk(target, args):
    """Installs the pinned Apple SDK where Chromium looks for a hermetic Xcode. It comes from a private bucket and
    is never published or packaged (specs/0002-build.md, macOS)."""
    entry = target_entry(target).get('mac_sdk')
    if not entry:
        print(f'{target} needs no Apple SDK', flush=True)
        return
    # Where Chromium looks for a hermetic Xcode, or for an embedder's build, wherever it's asked.
    destination = Path(args.destination) if args.destination else checkout_root()/'src'/HERMETIC_XCODE
    marker = destination/'.chromium-sdk-apple-sdk'
    if marker.is_file() and marker.read_text() == entry['sha256']:
        print('mac-sdk: the pinned SDK is in place', flush=True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp)/'sdk.tar.zst'
        if args.archive:
            shutil.copyfile(args.archive, archive)
        else:
            run('gcloud', 'storage', 'cp', f'gs://{entry["bucket"]}/{entry["object"]}', archive)
        if sha256(archive) != entry['sha256']:
            raise SystemExit('the Apple SDK archive does not match its pin in targets.json')
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        shutil.rmtree(destination, ignore_errors=True)
        (destination/'Contents').mkdir(parents=True)
        zstd = subprocess.Popen(['zstd', '-dc', '-q', str(archive)], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=zstd.stdout, mode='r|') as stream:
            stream.extractall(destination/'Contents', filter='data')
        if zstd.wait():
            raise SystemExit('zstd failed')
    marker.write_text(entry['sha256'])
    summary(f'Apple SDK {entry["sdk"]}, from Xcode {entry["xcode"]}')


# patch

def patch_files(root=None):
    """Each patch and the repository it applies to, relative to src."""
    root = root or REPO/'chromium/patches'
    for path in sorted(root.glob('**/*.patch')):
        relative = path.relative_to(root).as_posix()[:-len('.patch')]
        yield path, '' if relative == 'src' else relative


def applied_dir():
    """Copies of the patches applied to the checkout, which revert them whatever the repository's patches are now."""
    return checkout_root()/'.chromium-sdk-applied'


def patched_files(patch):
    """The files a patch changes, relative to its repository."""
    return sorted({line.split(' b/', 1)[1] for line in patch.read_text().splitlines()
                   if line.startswith('diff --git a/')})


def git_repo(src, repo):
    return src/repo if repo else src


def reverts(src, repo, patch):
    return run('git', '-C', git_repo(src, repo), 'apply', '--reverse', '--check', patch, check=False, quiet=True) == 0


def restore(src, repo, patch):
    """Puts the files a patch touches back to the pin's: reverts it if it's applied, and otherwise checks them out,
    which also undoes a patch a stopped machine applied in part."""
    if reverts(src, repo, patch):
        run('git', '-C', git_repo(src, repo), 'apply', '--reverse', patch)
    else:
        run('git', '-C', git_repo(src, repo), 'checkout', '--', *patched_files(patch))


def unpatch(src):
    """Reverts the patches applied to the checkout, so a sync or another patch starts from the pin's files."""
    recorded = applied_dir()
    if not recorded.exists():
        return
    for path, repo in patch_files(recorded):
        if git_repo(src, repo).exists():
            restore(src, repo, path)
        path.unlink()
    shutil.rmtree(recorded)


def step_patch(target, args):
    """Applies each repository's patch, unless it's applied already, recording each as it goes, so a machine
    stopped between two repositories resumes where it was. Files whose contents come out the same keep their
    times, so a change to the patch rebuilds only what it changes."""
    src = checkout_root()/'src'
    wanted = dict((repo, path) for path, repo in patch_files())
    recorded = dict((repo, path) for path, repo in patch_files(applied_dir())) if applied_dir().exists() else {}
    for repo in sorted(set(wanted) | set(recorded)):
        want, had = wanted.get(repo), recorded.get(repo)
        if want and had and want.read_bytes() == had.read_bytes() and reverts(src, repo, want):
            continue
        if want and not had and reverts(src, repo, want):
            record(repo, want)
            continue
        names = set(patched_files(want) if want else []) | set(patched_files(had) if had else [])
        files = [git_repo(src, repo)/name for name in names]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files if path.is_file()}
        if had:
            restore(src, repo, had)
            had.unlink()
        elif want:
            # No record and not applied: the pin's files, or a part applied by a machine that stopped.
            run('git', '-C', git_repo(src, repo), 'checkout', '--', *patched_files(want))
        if want:
            # Recorded first, so a machine stopped mid-apply leaves a record of every file it may have changed.
            record(repo, want)
            run('git', '-C', git_repo(src, repo), 'apply', want)
        for path, (content, mtime) in before.items():
            if path.is_file() and path.read_bytes() == content:
                os.utime(path, ns=(mtime, mtime))
    print('patch: applied', flush=True)
    # Chromium's build sees only files in its tree. Copy rather than link, and only what changed, so a new
    # checkout of this repository rebuilds nothing.
    sdk_gn = src/GN_DIR
    if sdk_gn.is_symlink():
        sdk_gn.unlink()
    sdk_gn.mkdir(exist_ok=True)
    names = {path.name for path in (REPO/'chromium/gn').iterdir()}
    for stale in sdk_gn.iterdir():
        if stale.name not in names:
            stale.unlink()
    for path in (REPO/'chromium/gn').iterdir():
        copy = sdk_gn/path.name
        if not copy.exists() or copy.read_bytes() != path.read_bytes():
            shutil.copyfile(path, copy)


def record(repo, patch):
    """Keeps a copy of a patch as applied, which reverts it whatever this repository's patches are later."""
    copy = applied_dir()/(f'{repo}.patch' if repo else 'src.patch')
    copy.parent.mkdir(parents=True, exist_ok=True)
    # Whole or not at all: a record a stopped machine cut short would revert only part of what's applied.
    partial = copy.with_name(copy.name+'.partial')
    shutil.copyfile(patch, partial)
    os.replace(partial, copy)


# configure

def args_gn(target):
    """The build's args.gn: the shared settings, then the target's system and processor."""
    entry = target_entry(target)
    lines = [(REPO/'chromium/args.gn').read_text().rstrip('\n'), '', '# The target (targets.json).',
             f'target_os = "{entry["target_os"]}"', f'target_cpu = "{entry["target_cpu"]}"']
    if entry['target_os'] == 'mac':
        plist = (checkout_root()/'src'/HERMETIC_XCODE/'Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/'
                 'MacOSX.sdk/System/Library/CoreServices/SystemVersion.plist')
        import plistlib
        version = plistlib.loads(plist.read_bytes())
        for kind in ('official', 'official_universal'):
            lines += [f'mac_sdk_{kind}_version = "{version["ProductVersion"]}"',
                      f'mac_sdk_{kind}_build_version = "{version["ProductBuildVersion"]}"']
        # A Linux machine can't run the target's build to make V8's context snapshot, nor fetch dsymutil.
        lines += ['use_system_xcode = false', 'use_v8_context_snapshot = false', 'enable_dsyms = false']
    return '\n'.join(lines)+'\n'


def step_configure(target, args):
    directory = out_dir(target)
    directory.mkdir(parents=True, exist_ok=True)
    wanted = args_gn(target)
    current = directory/'args.gn'
    # A restored build directory with the same arguments is left alone: a newer args.gn regenerates everything.
    if current.exists() and current.read_text() == wanted and (directory/'build.ninja').exists():
        print('configure: arguments unchanged', flush=True)
        return
    current.write_text(wanted)
    run('gn', 'gen', directory, cwd=checkout_root()/'src')


# build

def step_build(target, args):
    jobs = ['-j', str(args.jobs)] if args.jobs else []
    run('autoninja', *jobs, '-C', out_dir(target), 'chromium_sdk', cwd=checkout_root()/'src')


def step_package(target, args):
    import package
    package.package(target, checkout_root(), out_dir(target), args.version, Path(args.output))


def step_sources(target, args):
    """The corresponding source of the target's copyleft components (specs/0004-licences.md). With --sysroots, also
    Debian's source packages for every Linux target's sysroot, and this repository's source."""
    import licences
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    written = licences.source_archives(checkout_root()/'src', out_dir(target), output, args.version)
    if args.sysroots:
        linux = [(entry['sysroot'], 'build/linux/sysroot_scripts/generated_package_lists/bullseye.' +
                  {'x64': 'amd64', 'arm64': 'arm64'}[entry['target_cpu']])
                 for entry in targets().values() if entry.get('sysroot')]
        # Not on the kept disk, which holds only the build: the job downloads them each time.
        cache = Path(args.cache) if args.cache else Path(tempfile.gettempdir())/'chromium-sdk-debian-cache'
        written.append(licences.sysroot_sources(checkout_root()/'src', linux, cache, output, args.version,
                                                'debian-bullseye-sysroots'))
        written.append(licences.sdk_source(output, args.version))
    for entry in written:
        summary(f'{entry["name"]}: {entry["size"]/1024**2:.0f} MiB')
        if entry['size'] >= 2*1024**3:
            raise SystemExit(f'{entry["name"]} is at or over the 2 GiB limit for a release asset')
    (output/f'assets-sources-{target}.json').write_text(json.dumps(written, indent=2)+'\n')


STEPS = {
    'checkout': step_checkout,
    'packages': step_packages,
    'mac-sdk': step_mac_sdk,
    'patch': step_patch,
    'configure': step_configure,
    'build': step_build,
    'package': step_package,
    'sources': step_sources,
}


def raise_file_limit():
    # A Chromium build holds many files open at once, and some machines allow 1024.
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = hard if hard != resource.RLIM_INFINITY else 65536
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('step', choices=STEPS)
    parser.add_argument('target', choices=targets())
    parser.add_argument('--jobs', type=int, help='build: parallel jobs, for a machine short of memory')
    parser.add_argument('--archive', help="mac-sdk: a local copy of the pinned archive, instead of the bucket's")
    parser.add_argument('--destination', help="mac-sdk: install it here rather than in the checkout; its SDK is "
                        "then <destination>/" + APPLE_SDK_IN_XCODE)
    parser.add_argument('--version', help='package: the version the package carries')
    parser.add_argument('--output', help='package, sources: the directory to write assets to')
    parser.add_argument('--cache', help="sources: where to keep Debian's downloads (default: the system's temp)")
    parser.add_argument('--sysroots', action='store_true',
                        help="sources: also the Linux sysroots' Debian sources and this repository's")
    args = parser.parse_args(argv)
    if args.step in ('package', 'sources') and not (args.version and args.output):
        parser.error(f'{args.step} needs --version and --output')
    raise_file_limit()
    STEPS[args.step](args.target, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
