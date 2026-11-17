#!/usr/bin/env python3
"""Checks that need no Chromium: the repository's files agree with each other (specs/0003-ci-cd.md, CI)."""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# An action's reference, bare or quoted as YAML allows, without its quotes.
PINNED = re.compile(r'^\s*(?:-\s+)?uses:\s+(["\']?)([^\s@#"\']+)@([0-9a-f]{40})\1(?:\s|$)')
USES = re.compile(r'^\s*(?:-\s+)?uses:\s+(?:"([^"\s]+)"|\'([^\'\s]+)\'|([^\s"\']+))')


TARGET_WORKFLOW = './.github/workflows/target.yml'
CHECKS_WORKFLOW = './.github/workflows/checks.yml'
FORK_GUARD = 'github.event.pull_request.head.repo.full_name == github.repository'
PUBLISHES = "needs.version.outputs.publish == 'true'"
BUILD_RUNNER = 'lanescape-80vcpu-linux-amd64-spot'
# The build's work disk (specs/0003-ci-cd.md, Caches): as large as a checkout and a build directory need, and as fast as
# moving their many small files in and out of caches needs.
BUILD_DISK = ('iops: 10000', 'size-gb: 150', 'throughput-mibps: 2400')
READ = {'contents': 'read'}


KEY_LINE = re.compile(r'( *)(- +)?([A-Za-z0-9_-]+):(?: +(.*))?')
ITEM_LINE = re.compile(r'( *)- +(.*)')
QUOTED = re.compile(r'''("(?:[^"\\]|\\.)*"|'(?:[^']|'')*')(\s+#.*)?''')
FLOW_SEQUENCE = re.compile(r'''\[[^\[\]{}:&*!"'#]*\](\s+#.*)?''')


def yaml_subset_problems(path, relative):
    """The YAML that the workflows and actions are written in, so that the line-based reading below sees exactly what
    GitHub does: block mappings and sequences, each level indented 2 spaces more than its parent; plain keys followed
    directly by a colon; values that are plain, quoted on one line, `{}`, a flat flow sequence or a block scalar; and
    no anchors, aliases, tags, merge keys, flow mappings, directives or documents."""
    found = []
    stack, child, block = [0], False, None
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if block is not None:
            if not line.strip() or len(line) - len(line.lstrip(' ')) > block:
                continue
            block = None
        if not line.strip() or line.lstrip(' ').startswith('#'):
            continue
        key, item = KEY_LINE.fullmatch(line), ITEM_LINE.fullmatch(line)
        if not key and not item:
            found.append(f'{relative}:{number}: write `key: value`, with a plain key and no space before its colon, '
                         'or `- item`')
            continue
        indent = len((key or item).group(1))
        if child and indent > stack[-1] - 2:
            # The parent key's mapping or sequence; a line no further in than the key leaves it null.
            if indent != stack[-1]:
                found.append(f'{relative}:{number}: indent a mapping or sequence 2 spaces more than its parent')
        else:
            while len(stack) > 1 and stack[-1] > indent:
                stack.pop()
            if stack[-1] != indent:
                found.append(f'{relative}:{number}: indent a mapping or sequence 2 spaces more than its parent')
        child = False
        value = (key.group(4) if key else item.group(2)) or ''
        # The column of a key, which is further in after a sequence's `- `.
        column = indent + len(key.group(2)) if key and key.group(2) else indent
        if key and key.group(2):
            stack.append(column)
        if key and not value:
            stack.append(column + 2)
            child = True
        elif re.fullmatch(r'[|>][-+]?(\s+#.*)?', value):
            block = column
        elif value[0] in '&*!%@`?{[\'"|>-' and value != '{}' and not FLOW_SEQUENCE.fullmatch(value) and \
                not QUOTED.fullmatch(value):
            found.append(f'{relative}:{number}: use no anchors, aliases, tags, flow mappings, nested sequences or '
                         'multi-line quotes')
    return found


def in_block_scalars(lines):
    """The indexes of lines that are a block scalar's text, such as a step's script, which is never YAML."""
    inside, block = set(), None
    for index, line in enumerate(lines):
        if block is not None:
            if not line.strip() or len(line) - len(line.lstrip(' ')) > block:
                inside.add(index)
                continue
            block = None
        key = KEY_LINE.fullmatch(line)
        if key and re.fullmatch(r'[|>][-+]?(\s+#.*)?', key.group(4) or ''):
            block = len(key.group(1)) + len(key.group(2) or '')
    return inside


def step_lines(lines, index):
    """The lines of the sequence item, such as a step, that holds lines[index]: from its `- ` up to the next item or
    the end of its sequence."""
    column = len(lines[index]) - len(lines[index].lstrip(' -'))
    start = next(at for at in range(index, -1, -1)
                 if re.match(r' *- ', lines[at]) and lines[at].index('-') < column)
    dash = lines[start].index('-')
    end = next((at for at in range(start + 1, len(lines))
                if lines[at].strip() and len(lines[at]) - len(lines[at].lstrip(' ')) <= dash), len(lines))
    return lines[start:end]


def step_keys(step, within=None):
    """A step's own keys, as `key: value` lines, from step_lines(); or with `within`, the keys directly under that
    key of the step, such as its `with`."""
    dash = step[0].index('-')
    own = [(' ' * (dash + 2) + line[dash + 2:]) if at == 0 else line for at, line in enumerate(step)]
    keys = [line.strip() for line in own if len(line) - len(line.lstrip(' ')) == dash + 2 and line.strip()]
    if within is None:
        return keys
    found, inside = [], False
    for line in own:
        indent = len(line) - len(line.lstrip(' '))
        if line.strip() and indent <= dash + 2:
            inside = indent == dash + 2 and line.strip() == f'{within}:'
        elif inside and indent == dash + 4:
            found.append(line.strip())
    return found


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


SAVES_CHECKOUT = "if: inputs.target == 'linux-amd64' && !endsWith(steps.restored.outputs.checkout, '_exact')"
CHECKOUT_EXCLUDES = {'lost+found', '.libchromium-restored', 'chromium/src/out'}


def block_lines(step, key):
    """The lines of a literal block scalar, `|`, under `key:` directly in a step's `with`, stripped; a folded one,
    `>`, would join them into one."""
    dash = step[0].index('-')
    found, inside, column = [], False, None
    for line in step[1:]:
        indent = len(line) - len(line.lstrip(' '))
        if column is not None:
            if line.strip() and indent <= column:
                break
            if line.strip():
                found.append(line.strip())
        elif line.strip() and indent <= dash + 2:
            inside = indent == dash + 2 and line.strip() == 'with:'
        elif inside and indent == dash + 4 and re.fullmatch(rf' *{re.escape(key)}: *\|[-+]?', line):
            column = indent
    return found


def service_step_problems(relative, number, lines, job, action, saves):
    """The runner service's work disk and caches (specs/0003-ci-cd.md, Caches): only target.yml's build has them,
    and only linux-amd64 saves the checkout, once for each key, so no cache ever holds the Apple SDK, which the
    macOS build puts in its checkout."""
    found = []
    step = step_lines(lines, number - 1)
    given = step_keys(step, 'with')
    if action not in ('disk', 'cache', 'save-cache'):
        return [f'{relative}:{number}: the runner service keeps no disks; use lanescape/runners\' actions disk, '
                'cache and save-cache']
    if relative != '.github/workflows/target.yml' or job != 'build':
        return [f'{relative}:{number}: only target.yml\'s build has a work disk and caches']
    if action == 'disk':
        saves.append('disk')
        if sorted(given) != list(BUILD_DISK) or any(key.startswith('if:') for key in step_keys(step)):
            found.append(f'{relative}:{number}: the build asks for its work disk, every time, as '
                         f'{", ".join(BUILD_DISK)}')
    if action == 'save-cache' and 'name: chromium-out-${{ inputs.target }}' in given:
        saves.append('chromium-out')
        if sorted(given) != ['name: chromium-out-${{ inputs.target }}',
                             'path: /work/chromium/src/out/${{ inputs.target }}'] or \
                any(key.startswith('if:') for key in step_keys(step)):
            found.append(f'{relative}:{number}: save the build directory alone, whole, every time, '
                         'from /work/chromium/src/out/${{ inputs.target }}')
    elif action == 'save-cache':
        saves.append('chromium-checkout')
        # Anything else it saves is the checkout, as below.
        conditions = [key for key in step_keys(step) if key.startswith('if:')]
        if conditions != [SAVES_CHECKOUT] or 'name: chromium-checkout' not in given or 'path: /work' not in given or \
                set(block_lines(step, 'exclude')) != CHECKOUT_EXCLUDES:
            found.append(f'{relative}:{number}: only linux-amd64 saves the checkout, from /work, when it wasn\'t '
                         f'restored for its key, {SAVES_CHECKOUT}, leaving out exactly '
                         + ', '.join(sorted(CHECKOUT_EXCLUDES)))
    return found


def workflow_problems(repo):
    """The workflows' shape (specs/0003-ci-cd.md, CI): one chain per target, build → embedder → test, each gated by
    the checks and failing alone, CD's publish needing every chain, and the OIDC token only where the Apple SDK is
    fetched."""
    found = []
    for path in yaml_files(repo):
        found += yaml_subset_problems(path, path.relative_to(repo).as_posix())
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
    for path in yaml_files(repo):
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
        if name == 'ci.yml' and set(jobs) != {'checks', *calls}:
            found.append('.github/workflows/ci.yml must have no jobs but checks and the calls of target.yml, so that a '
                         'fork\'s pull request runs only the checks')
        for job_name, job in calls.items():
            # Each chain needs the checks (and in CD the version) and nothing else, so it fails alone.
            needs = ['checks'] if name == 'ci.yml' else ['checks', 'version']
            if sorted(job['needs']) != needs:
                found.append(f'.github/workflows/{name}: {job_name} must need exactly ' + ' and '.join(needs))
            if name == 'ci.yml' and job.get('if') != FORK_GUARD:
                found.append(f'.github/workflows/{name}: {job_name} must run only for this repository\'s branches, '
                             f'if: {FORK_GUARD}')
            # CD's packages are whole, as published; CI's are quick (scripts/package.py), and never published.
            given = {key: value for key, value in job['with'].items() if key != 'target'}
            wanted = {'version': '${{ needs.version.outputs.version }}', 'release': 'true'} if name == 'cd.yml' else {}
            if given != wanted:
                found.append(f'.github/workflows/{name}: {job_name} must pass '
                             + (', '.join(f'{key}: {value}' for key, value in wanted.items()) or 'only its target'))
            if name == 'cd.yml' and job.get('if') != PUBLISHES:
                found.append(f'.github/workflows/{name}: {job_name} must run only if the version job says it publishes')
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

    saves = []
    for path in yaml_files(repo):
        relative = path.relative_to(repo).as_posix()
        lines = path.read_text().splitlines()
        scripts = in_block_scalars(lines)
        for number, line in enumerate(lines, 1):
            if line.strip().startswith('#'):
                continue
            if BUILD_RUNNER in line and relative != '.github/workflows/target.yml':
                found.append(f'{relative}:{number}: only target.yml\'s build runs on {BUILD_RUNNER}')
            if re.match(r'\s+id-token:', line) and relative not in ('.github/workflows/ci.yml',
                                                                     '.github/workflows/cd.yml'):
                found.append(f'{relative}:{number}: only ci.yml\'s and cd.yml\'s macOS calls of target.yml may grant '
                             'id-token')
            # A script's text is never a step.
            used = uses_of(line) if number - 1 not in scripts else None
            if used and used.startswith('actions/upload-artifact@'):
                # Artifacts are intermediate and kept a day (specs/0003-ci-cd.md, CI).
                given = step_keys(step_lines(lines, number - 1), 'with')
                if [key for key in given if key.startswith('retention-days:')] != ['retention-days: 1']:
                    found.append(f'{relative}:{number}: keep the artifact for 1 day, `retention-days: 1`')
            if not used:
                continue
            job = next((heading.group(1) for heading in (re.fullmatch(r'  ([A-Za-z0-9_-]+):\s*', other)
                                                              for other in reversed(lines[:number-1]))
                        if heading), None)
            # GitHub reads an owner and repository in any case.
            service = re.fullmatch(r'lanescape/runners/actions/([a-z-]+)@.*', used, re.IGNORECASE)
            if service and service.group(1).lower() != 'checkout':
                found += service_step_problems(relative, number, lines, job, service.group(1).lower(), saves)
            if used == './.github/actions/apple-sdk':
                # In its own step, guarded by its own condition, in a job its call's grant reaches.
                guarded = [key for key in step_keys(step_lines(lines, number - 1)) if key.startswith('if:')]
                if relative != '.github/workflows/target.yml' or job not in ('build', 'embedder') or \
                        guarded != ["if: inputs.target == 'macos-arm64'"]:
                    found.append(f'{relative}:{number}: only target.yml\'s build and embedder may fetch the Apple SDK, '
                                 'and only for macos-arm64')
    if saves.count('disk') != 1:
        found.append('.github/workflows/target.yml: its build must ask for its work disk once')
    saves = [what for what in saves if what != 'disk']
    if sorted(saves) != ['chromium-checkout', 'chromium-out']:
        found.append('.github/workflows/target.yml: its build must save the build directory once and the checkout once')
    return found


CACHE_NAME = re.compile(r'[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?')


def cache_problems(repo):
    """.github/runners.json's caches (specs/0003-ci-cd.md, Caches): the checkout, keyed by what decides its files,
    and each target's build directory, keyed by everything that decides what its build compiles."""
    found = []
    targets = json.loads((repo/'targets.json').read_text())
    runners = json.loads((repo/'.github/runners.json').read_text())
    if 'disks' in runners:
        found.append('.github/runners.json: the runner service keeps no disks; declare "caches"')
    if set(runners) - {'caches', 'branches'}:
        found.append('.github/runners.json has fields the runner service refuses')
    caches = runners.get('caches', {})
    for name in caches:
        if not CACHE_NAME.fullmatch(name):
            found.append(f'.github/runners.json: {name} is not a cache name: 1 to 32 of a-z, 0-9 and -, starting with '
                         'a letter and not ending with -')
    if set(caches) != {'chromium-checkout', *(f'chromium-out-{target}' for target in targets)}:
        found.append('.github/runners.json must declare chromium-checkout and one chromium-out-<target> for each '
                     'target in targets.json, and no other caches')
    patches = sorted(p.relative_to(repo).as_posix() for p in (repo/'chromium/patches').glob('**/*.patch'))
    gn = sorted(p.relative_to(repo).as_posix() for p in (repo/'chromium/gn').iterdir())
    # The checkout's files: the pin, .gclient, and the patches, which it's saved with.
    checkout = sorted({'chromium/pin.json', 'chromium/gclient', *patches})
    # Everything that decides what a target's build compiles: its settings, sources, patch and arguments.
    out = sorted({*checkout, 'chromium/args.gn', 'targets.json', 'scripts/sdk.py', *gn})
    for name, declaration in caches.items():
        listed = declaration.get('from', []) if isinstance(declaration, dict) else []
        for path in listed:
            if not (repo/path).is_file():
                found.append(f'.github/runners.json: {name} names {path}, which does not exist')
        keyed = checkout if name == 'chromium-checkout' else out
        if sorted(listed) != keyed:
            found.append(f'.github/runners.json: {name} must be keyed by exactly {", ".join(keyed)}')
    return found


def yaml_files(repo):
    """Every YAML file under .github: .yml, and the .yaml that GitHub reads as a workflow or an action."""
    github = repo/'.github'
    return sorted(set(github.glob('**/*.yml')) | set(github.glob('workflows/*.yaml')) | set(github.glob('**/action.yaml')))


def uses_of(line):
    """The action a workflow's line uses, without quotes, or None."""
    match = USES.match(line)
    return next(group for group in match.groups() if group) if match else None


def uses_problems(relative, number, line):
    """How a workflow's line names an action: the runner service's, lanescape/runners/actions/<name>, by its tag,
    `@v1`, which moves only forward, once a reviewed merge there is deployed; every other action pinned to a commit."""
    used = uses_of(line)
    if used and '\\' in used:
        # YAML would decode an escape into another reference than the one these rules read.
        return [f'{relative}:{number}: write the action\'s reference without escapes']
    if used and used.startswith('./') and (not used.startswith('./.github/') or '..' in used.split('/')):
        # Every local action and workflow is where these rules read them.
        return [f'{relative}:{number}: keep {used} under .github/']
    if not used or used.startswith('./'):
        return []
    # The owner and repository, with or without a path in it.
    repository = '/'.join(used.split('@')[0].split('/')[:2]).lower()
    if repository == 'lanescape/runners':
        if not re.fullmatch(r'lanescape/runners/actions/[a-z-]+@v1', used):
            return [f'{relative}:{number}: use {used.split("@")[0]}@v1, lanescape/runners\' tag']
        return []
    if not PINNED.match(line):
        return [f'{relative}:{number}: pin {used} to a commit']
    return []


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
    found += cache_problems(REPO)

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

    for path in yaml_files(REPO):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            found += uses_problems(path.relative_to(REPO), number, line)

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
