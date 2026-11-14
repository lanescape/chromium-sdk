#!/usr/bin/env python3
"""Checks that need no Chromium: the repository's files agree with each other (specs/0003-ci-cd.md, CI)."""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PINNED = re.compile(r'^\s*(?:-\s+)?uses:\s+([^\s@#]+)@([0-9a-f]{40})\b')
USES = re.compile(r'^\s*(?:-\s+)?uses:\s+(\S+)')


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

    for path in sorted((REPO/'.github').glob('**/*.yml')):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = USES.match(line)
            if not match or match.group(1).startswith('./'):
                continue
            if not PINNED.match(line):
                found.append(f'{path.relative_to(REPO)}:{number}: pin {match.group(1)} to a commit')

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
