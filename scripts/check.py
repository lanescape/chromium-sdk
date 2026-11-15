#!/usr/bin/env python3
"""Checks that need no Chromium: the repository's files agree with each other (specs/0003-ci-cd.md, CI)."""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PINNED = re.compile(r'^\s*(?:-\s+)?uses:\s+([^\s@#]+)@([0-9a-f]{40})\b')
USES = re.compile(r'^\s*(?:-\s+)?uses:\s+(\S+)')


TARGET_WORKFLOW = './.github/workflows/target.yml'
CHECKS_WORKFLOW = './.github/workflows/checks.yml'
FORK_GUARD = 'github.event.pull_request.head.repo.full_name == github.repository'
PUBLISHES = "needs.version.outputs.publish == 'true'"
BUILD_RUNNER = 'lanescape-80vcpu-linux-amd64-spot'
READ = {'contents': 'read'}


def workflow_jobs(path):
    """A workflow's jobs, read from its lines as this repository writes them: each job's own keys, such as `uses`,
    `if` and `runs-on`, its `needs` as a list, its `permissions` (None if it sets none; an inline value other than
    `{}` is kept whole under '', so it never matches a map) and its `with`."""
    jobs, job, section = {}, None, None
    inside = False
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line.startswith(' '):
            inside = line.rstrip() == 'jobs:'
            job = None
            continue
        if not inside:
            continue
        if match := re.fullmatch(r'  ([A-Za-z0-9_-]+):\s*', line):
            job = jobs[match.group(1)] = {'needs': [], 'permissions': None, 'with': {}}
            section = None
        elif job is not None and (match := re.fullmatch(r'    ([a-z-]+):\s*(.*?)\s*', line)):
            key, value = match.groups()
            section = key
            if key == 'needs':
                job['needs'] = [need.strip() for need in value.strip('[]').split(',') if need.strip()]
            elif key == 'permissions':
                job['permissions'] = {} if value in ('', '{}') else {'': value}
            elif key != 'with':
                job[key] = value
        elif job is not None and (match := re.fullmatch(r'      ([a-z-]+):\s*(.*?)\s*', line)):
            if section == 'permissions':
                job['permissions'][match.group(1)] = match.group(2)
            elif section == 'with':
                job['with'][match.group(1)] = match.group(2)
    return jobs


def top_level(path, key):
    """A workflow's top-level key's value as written on its line, or None if it has none."""
    values = re.findall(rf'^{key}:[ \t]*(.*?)\s*$', path.read_text(), re.M)
    return values[0] if values else None


def workflow_problems(repo):
    """The workflows' shape (specs/0003-ci-cd.md, CI): one chain per target, build → embedder → test, each gated by
    the checks and failing alone, CD's publish needing every chain, and the OIDC token only where the Apple SDK is
    fetched."""
    found = []
    workflows = repo/'.github/workflows'
    targets = json.loads((repo/'targets.json').read_text())
    workflow = {name: workflow_jobs(workflows/name) for name in ('checks.yml', 'ci.yml', 'cd.yml', 'target.yml')}

    # Permissions: none by default, `contents: read` for a job, with three exceptions. Each call of target.yml grants
    # its chain `contents: read`, and the macOS call also `id-token: write`; target.yml's build and embedder take
    # their call's grant; CD's publish job alone writes.
    for name in ('checks.yml', 'ci.yml', 'cd.yml'):
        if top_level(workflows/name, 'permissions') != '{}':
            found.append(f'.github/workflows/{name} must set `permissions: {{}}` for the whole workflow')
    if top_level(workflows/'target.yml', 'permissions') is not None:
        found.append('.github/workflows/target.yml must set no permissions for the whole workflow, so that its build '
                     'and embedder get their call\'s grant')
    for name, jobs in workflow.items():
        for job_name, job in jobs.items():
            if job.get('uses') == TARGET_WORKFLOW:
                target = job['with'].get('target')
                expected = {**READ, 'id-token': 'write'} if target == 'macos-arm64' else READ
            elif name == 'cd.yml' and job_name == 'publish':
                expected = {'contents': 'write'}
            elif name == 'target.yml' and job_name in ('build', 'embedder'):
                expected = None
            else:
                expected = READ
            if job['permissions'] != expected:
                what = 'no permissions of its own' if expected is None else 'exactly ' + ', '.join(
                    f'{key}: {value}' for key, value in expected.items())
                found.append(f'.github/workflows/{name}: {job_name} must have {what}')

    # Gating: a job runs only if every job it needs succeeded, and a failure is a failure.
    for path in sorted((repo/'.github').glob('**/*.yml')):
        relative = path.relative_to(repo).as_posix()
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith('#'):
                continue
            if re.search(r'\bcontinue-on-error:|\b(always|cancelled|failure)\(\)', line):
                found.append(f'{relative}:{number}: no job or step may run past a failure, or hide one')
    for name in ('ci.yml', 'cd.yml'):
        jobs = workflow[name]
        if jobs.get('checks', {}).get('uses') != CHECKS_WORKFLOW:
            found.append(f'.github/workflows/{name}: its checks job must call checks.yml')
        calls = {job_name: job for job_name, job in jobs.items() if job.get('uses') == TARGET_WORKFLOW}
        if sorted(job['with'].get('target', '') for job in calls.values()) != sorted(targets):
            found.append(f'.github/workflows/{name} must call target.yml once for each target in targets.json')
        for job_name, job in calls.items():
            if 'checks' not in job['needs']:
                found.append(f'.github/workflows/{name}: {job_name} must need checks')
            if name == 'ci.yml' and job.get('if') != FORK_GUARD:
                found.append(f'.github/workflows/{name}: {job_name} must run only for this repository\'s branches, '
                             f'if: {FORK_GUARD}')
            if name == 'cd.yml' and ('version' not in job['needs'] or job.get('if') != PUBLISHES):
                found.append(f'.github/workflows/{name}: {job_name} must need version and run only if it publishes')
        if name == 'cd.yml':
            publish = jobs.get('publish', {'needs': []})
            if not {'version', 'checks', *calls} <= set(publish['needs']) or 'if' in publish:
                found.append('.github/workflows/cd.yml: publish must need version, checks and every target\'s chain, '
                             'and run only if they all succeed')
    for job_name, job in workflow['checks.yml'].items():
        if 'if' in job:
            found.append(f'.github/workflows/checks.yml: {job_name} must always run')

    # The chain: build → embedder → test, the 80-vCPU machine for the build alone, the test on its target's machine.
    chain = workflow['target.yml']
    if set(chain) != {'build', 'embedder', 'test'} or \
            [chain[job]['needs'] for job in ('build', 'embedder', 'test')] != [[], ['build'], ['embedder']]:
        found.append('.github/workflows/target.yml: its jobs must be build, embedder needing build, and test needing '
                     'embedder')
    for job_name, job in chain.items():
        if 'if' in job:
            found.append(f'.github/workflows/target.yml: {job_name} must run only if the job it needs succeeded')
        if job_name != 'build' and BUILD_RUNNER in job.get('runs-on', ''):
            found.append(f".github/workflows/target.yml: only target.yml's build runs on {BUILD_RUNNER}")
    if chain.get('build', {}).get('runs-on') != BUILD_RUNNER:
        found.append(f'.github/workflows/target.yml: build must run on {BUILD_RUNNER}')
    runner = re.fullmatch(r"\$\{\{ fromJSON\('(\{.*\})'\)\[inputs\.target\] \}\}",
                          chain.get('test', {}).get('runs-on', ''))
    if not runner or json.loads(runner.group(1)) != {target: entry.get('test_runner')
                                                     for target, entry in targets.items()}:
        found.append('.github/workflows/target.yml: the test must run on targets.json\'s test_runner for its target')

    for path in sorted((repo/'.github').glob('**/*.yml')):
        relative = path.relative_to(repo).as_posix()
        lines = path.read_text().splitlines()
        for number, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            if BUILD_RUNNER in line and relative != '.github/workflows/target.yml':
                found.append(f'{relative}:{number}: only target.yml\'s build runs on {BUILD_RUNNER}')
            if re.match(r'\s+id-token:', line) and relative not in ('.github/workflows/ci.yml',
                                                                     '.github/workflows/cd.yml'):
                found.append(f'{relative}:{number}: only ci.yml\'s and cd.yml\'s macOS calls of target.yml may grant '
                             'id-token')
            match = USES.match(line)
            if match and match.group(1) == './.github/actions/apple-sdk':
                step = lines[max(0, number-3):number-1]
                if relative != '.github/workflows/target.yml' or \
                        not any(re.fullmatch(r"\s+if: inputs\.target == 'macos-arm64'", other) for other in step):
                    found.append(f'{relative}:{number}: only target.yml may fetch the Apple SDK, and only for '
                                 'macos-arm64')
    return found


def problems():
    found = []
    for path in sorted(list(REPO.glob('scripts/*.py')) + list(REPO.glob('tests/**/*.py'))):
        try:
            compile(path.read_text(), str(path), 'exec')
        except SyntaxError as error:
            found.append(f'{path.relative_to(REPO)} does not compile: {error}')

    pin = json.loads((REPO/'chromium/pin.json').read_text())
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', pin.get('tag', '')):
        found.append('chromium/pin.json: the tag is not a Chromium release, four numbers')
    if not re.fullmatch(r'[0-9a-f]{40}', pin.get('commit', '')):
        found.append('chromium/pin.json: the commit is not a full hash')

    targets = json.loads((REPO/'targets.json').read_text())
    runners = json.loads((REPO/'.github/runners.json').read_text())
    if set(runners) - {'disks', 'branches'}:
        found.append('.github/runners.json has fields the runner service refuses')
    disks = runners.get('disks', {})
    if set(disks) != set(targets):
        found.append('.github/runners.json must declare one disk family for each target in targets.json')
    patches = sorted(p.relative_to(REPO).as_posix() for p in (REPO/'chromium/patches').glob('**/*.patch'))
    gn = sorted(p.relative_to(REPO).as_posix() for p in (REPO/'chromium/gn').iterdir())
    # Everything that decides what a target's build compiles: its settings, sources, patch and arguments.
    keyed = sorted({'chromium/pin.json', 'chromium/gclient', 'chromium/args.gn', 'targets.json', 'scripts/sdk.py',
                    *gn, *patches})
    for family, declaration in disks.items():
        listed = declaration.get('from', [])
        for path in listed:
            if not (REPO/path).is_file():
                found.append(f'.github/runners.json: {family} names {path}, which does not exist')
        if sorted(listed) != keyed:
            found.append(f'.github/runners.json: {family} must be keyed by exactly {", ".join(keyed)}')

    # The workflows fetch the Apple SDK that targets.json pins, from the bucket's fixed layout.
    action = (REPO/'.github/actions/apple-sdk/action.yml').read_text()
    fetched = {key: re.findall(rf'^\s+{key}:\s*(\S+)\s*$', action, re.M) for key in ('OBJECT', 'SHA256')}
    pinned = [entry['mac_sdk'] for entry in targets.values() if entry.get('mac_sdk')]
    if [len(values) for values in fetched.values()] != [1, 1]:
        found.append('.github/actions/apple-sdk/action.yml must set OBJECT and SHA256 once each')
    elif {(sdk_pin['object'], sdk_pin['sha256']) for sdk_pin in pinned} != {(fetched['OBJECT'][0],
                                                                             fetched['SHA256'][0])}:
        found.append('.github/actions/apple-sdk/action.yml must fetch the Apple SDK targets.json pins')
    for sdk_pin in pinned:
        if not re.fullmatch(r'gs://lanescape-ci-tools/macos/sdk/[^/]+/[^/]+', sdk_pin['object']) or \
                not re.fullmatch(r'[0-9a-f]{64}', sdk_pin['sha256']):
            found.append('targets.json: mac_sdk must name gs://lanescape-ci-tools/macos/sdk/<version>/<file> '
                         'and its SHA-256')

    for path in sorted((REPO/'.github').glob('**/*.yml')):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = USES.match(line)
            if not match or match.group(1).startswith('./'):
                continue
            if not PINNED.match(line):
                found.append(f'{path.relative_to(REPO)}:{number}: pin {match.group(1)} to a commit')

    found += workflow_problems(REPO)

    readme = (REPO/'README.md').read_text()
    for spec in sorted((REPO/'specs').glob('*.md')):
        if f'specs/{spec.name}' not in readme:
            found.append(f'README.md does not list specs/{spec.name}')
    return found


def main():
    found = problems()
    for problem in found:
        print(problem)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
