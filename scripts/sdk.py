#!/usr/bin/env python3
"""Builds the SDK for one target: sdk.py <step> <target> [options] (specs/0002-build.md).

Steps, in order: checkout, packages, mac-sdk (macOS only), patch, configure, build, package, and for a release,
sources. Each is safe to run again on a kept checkout, such as one restored from a cache or resumed by the runner
service: it does nothing when its work is done, and a build carries on from what Siso finds. CI runs reconcile first,
after restoring the checkout and the build directory, which it keeps apart. The checkout is at $LIBCHROMIUM_CHECKOUT,
or ~/libchromium/checkout.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEPOT_TOOLS_URL = 'https://chromium.googlesource.com/chromium/tools/depot_tools.git'
CHROMIUM_URL = 'https://chromium.googlesource.com/chromium/src.git'
HERMETIC_XCODE = 'build/mac_files/xcode_binaries'
APPLE_SDK_IN_XCODE = 'Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
GN_DIR = 'libchromium'
BUILT_FROM = '.libchromium-built-from.json'


def checkout_root():
    return Path(os.environ.get('LIBCHROMIUM_CHECKOUT', Path.home()/'libchromium/checkout')).resolve()


def depot_tools():
    return Path(os.environ.get('DEPOT_TOOLS', checkout_root().parent/'depot_tools'))


def env():
    environment = dict(os.environ)
    environment['PATH'] = os.pathsep.join((str(depot_tools()), environment['PATH']))
    environment.update(DEPOT_TOOLS_UPDATE='0', DEPOT_TOOLS_METRICS='0', GCLIENT_SUPPRESS_GIT_VERSION_WARNING='1')
    # Chromium's dependencies in Cloud Storage are public, and read without
    # signing in. depot_tools' gsutil.py otherwise runs gsutil inside
    # `luci-auth context`, which on a Compute Engine machine signs in as the
    # machine's own account, and Cloud Storage refuses that account's narrow
    # scopes rather than reading anonymously. SWARMING_HEADLESS, which only
    # gsutil.py reads, makes it run gsutil directly, and an empty boto
    # configuration, in place of /etc/boto.cfg and ~/.boto, gives gsutil no
    # credentials of its own.
    environment.update(SWARMING_HEADLESS='1', BOTO_CONFIG=os.devnull)
    # Chromium's bindgen wrapper refuses to run with TARGET set, as Cargo's tools read it, and the workflows set
    # it for their own steps.
    environment.pop('TARGET', None)
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
    return checkout_root()/'.libchromium-checkout.json'


def moving_record():
    """Marks a move to another pin that hasn't finished: written before src leaves its pin, and removed once the
    sync and the hooks are done, so a machine stopped after src reached the new pin still syncs as for a move. A
    sync while any move is unfinished, whatever pin it was for, is a move, so a move abandoned part-way, even for
    another that was abandoned too, is never taken for a finished one."""
    return checkout_root()/'.libchromium-moving'


def wanted_checkout():
    return {'pin': pin(), 'gclient': gclient_digest()}


def ensure_depot_tools():
    tools = depot_tools()
    if not (tools/'gclient').exists():
        # Into a new directory, then renamed, so a clone a stopped machine cut short is never taken for one.
        partial = tools.with_name(tools.name+'.partial')
        shutil.rmtree(partial, ignore_errors=True)
        run('git', 'clone', '--depth', '1', DEPOT_TOOLS_URL, partial)
        shutil.rmtree(tools, ignore_errors=True)
        partial.rename(tools)
    # Its tools, gn among them, run on its own Python, which only a bootstrap fetches. gclient would bootstrap it
    # as it updates depot_tools, which DEPOT_TOOLS_UPDATE=0 stops. ensure_bootstrap fetches only what's missing,
    # so it runs every time: a bootstrap a stopped machine cut short on a kept disk is finished, and no marker it
    # writes part-way is taken for a whole one.
    run(tools/'ensure_bootstrap')
    # Its exit status doesn't say whether it worked: it waits for its background jobs without checking them, and
    # ends on commands that always succeed. So what the steps need is run, and fails the step if it can't.
    # vpython3 is run from depot_tools itself, so it takes depot_tools' own environment, as gclient does, and no
    # .vpython3 of a directory around it.
    run(tools/'python-bin/python3', '-c', 'pass')
    run(tools/'vpython3', '-c', 'pass', cwd=tools)


def step_checkout(target, args):
    root = checkout_root()
    root.mkdir(parents=True, exist_ok=True)
    ensure_depot_tools()
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
    moving = moving_record()
    if head != commit:
        moving.touch()
        # Patched files would stop the checkout; they're the pin's own files with the SDK's patch applied.
        unpatch(src)
        run('git', '-C', src, 'checkout', '-q', '-f', '--detach', commit)
    sync = ('gclient', 'sync', '--nohooks', '--no-history')
    if moving.exists():
        # A move to another pin, in place, so that most files that didn't change keep their times and a build
        # directory built from the old pin, restored with it, rebuilds little more than what changed: --reset
        # discards what a patch left in a dependency's files, and -D deletes the git dependencies the new pin no
        # longer has, which gclient would otherwise keep for good. Not --force at first: with -D, it deletes what
        # git status lists after a reset, in src every dependency whose commit the move changes, which would all be
        # cloned again with new times. But with --force gclient mends some dependencies it can't otherwise bring to
        # the pin, such as one whose HEAD is damaged, cloning some of them again, so a move that fails is tried once
        # more with it.
        if run(*sync, '--reset', '-D', '-r', f'src@{commit}', cwd=root, check=False) != 0:
            try:
                summary('gclient couldn\'t move the checkout to the pin in place, so it tries again with `--force`, '
                        'which may clone again dependencies it still has to move')
            except OSError:
                pass  # The summary is for people; the retry mustn't wait on it.
            run(*sync, '--force', '--reset', '-D', '-r', f'src@{commit}', cwd=root)
    else:
        # At the same pin, for another .gclient, with no move unfinished.
        run(*sync, '--force', '-r', f'src@{commit}', cwd=root)
    run('gclient', 'runhooks', cwd=root)
    # Before the record, so a record never stands beside a marker: a rerun that finds neither syncs again, as
    # for the same pin, which the move has finished.
    moving.unlink(missing_ok=True)
    partial = record_path.with_name(record_path.name+'.partial')
    partial.write_text(json.dumps(wanted)+'\n')
    os.replace(partial, record_path)


# packages

def step_packages(target, args):
    """Chromium's Linux build dependencies, from its own list. Not stamped: they live on the machine, which may be
    new, while the checkout is kept."""
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
    """Installs the pinned Apple SDK where Chromium looks for a hermetic Xcode. It comes from the organization's
    private bucket for CI tools, or a local copy of it (--archive), and is never published or packaged
    (specs/0002-build.md, macOS)."""
    entry = target_entry(target).get('mac_sdk')
    if not entry:
        print(f'{target} needs no Apple SDK', flush=True)
        return
    # Where Chromium looks for a hermetic Xcode, or for an embedder's build, wherever it's asked.
    destination = Path(args.destination) if args.destination else checkout_root()/'src'/HERMETIC_XCODE
    marker = destination/apple_sdk_marker().name
    if marker.is_file() and marker.read_text() == entry['sha256']:
        print('mac-sdk: the pinned SDK is in place', flush=True)
        return
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp)/'sdk.tar.zst'
        if args.archive:
            # CI copies the archive from the bucket and checks it first (.github/actions/apple-sdk).
            shutil.copyfile(args.archive, archive)
        else:
            # The organization's private bucket for CI tools, with the machine's own Google Cloud sign-in.
            run('gcloud', 'storage', 'cp', entry['object'], archive)
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
    return checkout_root()/'.libchromium-applied'


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


# reconcile

def checkout_state():
    """What a build directory built in the checkout is built from, as the checkout's own records say: the pin and
    .gclient it was synced to, each patch applied, with the files it changes, and the GN target's copy. None if the
    checkout isn't whole. The same state means the same files, whatever their times."""
    try:
        synced = json.loads(checkout_record().read_text())
    except (OSError, ValueError):
        return None
    patches = {}
    if applied_dir().exists():
        for path, repo in patch_files(applied_dir()):
            patches[repo] = {'sha256': sha256(path),
                             'files': [f'{repo}/{name}' if repo else name for name in patched_files(path)]}
    sdk_gn = checkout_root()/'src'/GN_DIR
    gn = {path.name: sha256(path) for path in sorted(sdk_gn.iterdir()) if path.is_file()} if sdk_gn.is_dir() else {}
    return {'synced': synced, 'patches': patches, 'gn': gn}


def apple_sdk_marker():
    return checkout_root()/'src'/HERMETIC_XCODE/'.libchromium-apple-sdk'


def built_from(target):
    """The record configure keeps in the build directory: the checkout's state, and for macOS the Apple SDK, which
    is installed in the checkout but never kept with it, so reconcile compares it with the pin instead."""
    marker = apple_sdk_marker()
    sdk_pin = marker.read_text() if target_entry(target).get('mac_sdk') and marker.is_file() else None
    return {'checkout': checkout_state(), 'apple_sdk': sdk_pin}


def well_formed_record(record):
    """Whether a build directory's record is one built_from wrote, with plain relative paths alone."""
    def relative(name):
        return isinstance(name, str) and name and not name.startswith('/') and \
            all(part not in ('', '.', '..') for part in name.split('/'))
    try:
        built = record['checkout']
        return isinstance(record, dict) and (record['apple_sdk'] is None or isinstance(record['apple_sdk'], str)) \
            and isinstance(built['synced'], dict) and isinstance(built['gn'], dict) and \
            all(relative(name) for name in built['gn']) and isinstance(built['patches'], dict) and \
            all(isinstance(patch['sha256'], str) and isinstance(patch['files'], list) and
                all(relative(name) for name in patch['files']) for patch in built['patches'].values())
    except (TypeError, KeyError, AttributeError):
        return False


def step_reconcile(target, args):
    """CI keeps the checkout and the target's build directory as caches of their own, restored apart
    (specs/0003-ci-cd.md, Caches), so the build directory may have been built from another checkout, whose differing
    files can be older than what was built from them, and would never rebuild. Run after restoring the two and before
    any other step: if the build directory was built from another state of the checkout, the files that differ are
    made newer than anything built, so they rebuild. Those are the files of each patch that differs and the GN
    target's copy, at the same pin and .gclient; any other difference, or another Apple SDK than the pinned one
    that mac-sdk installs, can't be told file by file, so the build directory goes, and the build starts afresh."""
    directory = out_dir(target)
    if not directory.exists():
        print('reconcile: no build directory', flush=True)
        return
    try:
        record = json.loads((directory/BUILT_FROM).read_text())
    except (OSError, ValueError):
        record = None
    built = record.get('checkout') if well_formed_record(record) else None
    state = checkout_state()
    sdk_pin = (target_entry(target).get('mac_sdk') or {}).get('sha256')
    if built is None or state is None or built['synced'] != state['synced'] or record['apple_sdk'] != sdk_pin:
        shutil.rmtree(directory)
        summary(f'The restored build directory was built from another Chromium checkout: {target} builds afresh')
        return
    if built == state:
        print('reconcile: the build directory was built from this checkout', flush=True)
        return
    names = set()
    for repo in set(built['patches']) | set(state['patches']):
        had, has = built['patches'].get(repo, {}), state['patches'].get(repo, {})
        if had.get('sha256') != has.get('sha256'):
            names |= set(had.get('files', [])) | set(has.get('files', []))
    if built['gn'] != state['gn']:
        names |= {f'{GN_DIR}/{name}' for name in set(built['gn']) | set(state['gn'])}
    src = checkout_root()/'src'
    now = time.time_ns()
    for name in sorted(names):
        path = src/name
        if path.is_file() and not path.is_symlink():
            os.utime(path, ns=(now, now))
    # configure rewrites the build directory's record.
    summary(f'The restored build directory was built with other patches or GN target: {len(names)} files of {target} '
            'rebuild')


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
    # What the build directory is now built from, for reconcile, once a cache has kept it apart from its checkout.
    record = directory/BUILT_FROM
    partial = record.with_name(record.name+'.partial')
    partial.write_text(json.dumps(built_from(target), indent=2, sort_keys=True)+'\n')
    os.replace(partial, record)
    wanted = args_gn(target)
    current = directory/'args.gn'
    # A restored build directory with the same arguments is left alone: a newer args.gn regenerates everything.
    if current.exists() and current.read_text() == wanted and (directory/'build.ninja').exists():
        print('configure: arguments unchanged', flush=True)
        return
    current.write_text(wanted)
    run('gn', 'gen', directory, cwd=checkout_root()/'src')


# build

SISO_EXPLAIN = 'siso_explain'
# Siso's two reasons for a step, as it writes them: an output older than an input, and a step whose dependency ran.
NEWER_INPUT = re.compile(r'output (.+) older than most recent input (.+): out:\S* in:\S*')
DIRTY_DEPENDENCY = re.compile(r'deps for (.+) dirty: input (.+): dirty')
# Enough to show what dominates; the artifact keeps the whole record.
REASONS_SHOWN = 12
REASONS_COUNTED = 1000


def where(path):
    """Up to four directories of the file Siso names, relative to the checkout's src, so that what rebuilds for one
    reason counts together."""
    parts = [part for part in path.split('/') if part not in ('', '.', '..')]
    return '/'.join(parts[:-1][:4]) or '.'


def explain_line(line):
    """Why Siso ran one step, as the job's summary counts it. Only these fixed words and a directory: the record
    itself is kept as an artifact."""
    if match := NEWER_INPUT.fullmatch(line):
        return f'an input is newer, under {where(match.group(2))}'
    if match := DIRTY_DEPENDENCY.fullmatch(line):
        return f'a dependency ran, under {where(match.group(2))}'
    return 'another reason (the record has it)'


def kept_explanation(target):
    """Where the build keeps Siso's record for the artifact siso-explain-<target>, or None outside a job."""
    temp = os.environ.get('RUNNER_TEMP')
    return Path(temp)/f'siso-explain-{target}'/f'{SISO_EXPLAIN}-{target}.txt' if temp else None


def explain(target):
    """Says why Siso ran what it ran in this build, from its own record: how many entries, and the commonest reasons
    in the job's summary; and keeps the record for the run's artifact siso-explain-<target>
    (specs/0003-ci-cd.md, CI)."""
    record = out_dir(target)/SISO_EXPLAIN
    if not record.is_file():
        summary(f'Siso explained nothing for {target}')
        return
    # Kept first, so the artifact has the record even if the summary can't be written. Whole or not at all: a copy
    # cut short is never uploaded as the record.
    copy = kept_explanation(target)
    if copy:
        copy.parent.mkdir(parents=True, exist_ok=True)
        partial = copy.with_name(copy.name+'.partial')
        try:
            shutil.copyfile(record, partial)
            os.replace(partial, copy)
        finally:
            partial.unlink(missing_ok=True)
    entries, uncounted, reasons = 0, 0, {}
    with open(record, encoding='utf-8', errors='replace') as lines:
        for line in lines:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            entries += 1
            reason = explain_line(line)
            if reason in reasons or len(reasons) < REASONS_COUNTED:
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                uncounted += 1
    summary(f'Siso explained {entries} entries for {target}:')
    for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:REASONS_SHOWN]:
        summary(f'{count}: {reason}')
    if uncounted:
        summary(f'{uncounted}: reasons past the first {REASONS_COUNTED} kinds, not counted (the record has them)')


def step_build(target, args):
    jobs = ['-j', str(args.jobs)] if args.jobs else []
    # A restored build directory holds the last build's record, and only this build's may be reported. If it can't be
    # removed, the build still runs, unexplained, and with no record, which Siso could otherwise fail to start for.
    try:
        (out_dir(target)/SISO_EXPLAIN).unlink(missing_ok=True)
        if copy := kept_explanation(target):
            copy.unlink(missing_ok=True)
        fresh, unexplained = True, []
    except OSError:
        fresh, unexplained = False, ['-explain_log=']
    try:
        run('autoninja', *jobs, *unexplained, '-C', out_dir(target), 'libchromium', cwd=checkout_root()/'src')
    finally:
        # Only a report, whether the build passed or failed: nothing in it may fail a build, or replace the error a
        # failed one raised.
        try:
            if fresh:
                explain(target)
            else:
                summary(f'Siso\'s last record for {target} could not be removed, so this build is not explained')
        except Exception:
            try:
                print('Could not say why Siso ran what it ran', flush=True)
            except Exception:
                pass


def step_package(target, args):
    import package
    package.package(target, checkout_root(), out_dir(target), args.version, Path(args.output), quick=args.quick)


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
        # Never in a cache, which holds only the build: the job downloads them each time.
        cache = Path(args.cache) if args.cache else Path(tempfile.gettempdir())/'libchromium-debian-cache'
        written.append(licences.sysroot_sources(checkout_root()/'src', linux, cache, output, args.version,
                                                'debian-bullseye-sysroots'))
        written.append(licences.sdk_source(output, args.version))
    for entry in written:
        summary(f'{entry["name"]}: {entry["size"]/1024**2:.0f} MiB')
        if entry['size'] >= 2*1024**3:
            raise SystemExit(f'{entry["name"]} is at or over the 2 GiB limit for a release asset')
    (output/f'assets-sources-{target}.json').write_text(json.dumps(written, indent=2)+'\n')


STEPS = {
    'reconcile': step_reconcile,
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
    parser.add_argument('--quick', action='store_true',
                        help="package: CI's package for its tests alone, quicker to compress and with no symbols; "
                        "only for a pull request's version, which is never published")
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
