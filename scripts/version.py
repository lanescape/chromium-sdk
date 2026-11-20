#!/usr/bin/env python3
"""An SDK version, from the pin and the repository's release tags (specs/0003-ci-cd.md, Versions).

    version.py pr <number>    prints <Chromium tag>-pr<number>, a version that is never published
    version.py main           prints JSON: the version a push to main publishes, or why it publishes none

A version is <Chromium tag>-<revision>, tagged v<version> when its release is published. Every commit CD runs for,
a push's newest, gets the next revision of its pin: the highest revision tagged for that Chromium tag, plus one, or
1. Versions only move forward, so a commit publishes only if it is newer than every released commit: a strict
descendant of every release tag. Tags and the whole history must have been fetched; `main` refuses a shallow
history, whose missing commits could make it skip a release it should publish.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TAG = re.compile(r'^v(\d+\.\d+\.\d+\.\d+)-([1-9]\d*)$')


def git(*args, check=True):
    result = subprocess.run(['git', '-C', str(REPO), *args], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    if check and result.returncode:
        raise SystemExit(f'git {" ".join(args)} failed')
    return result


def chromium_tag(commit='HEAD'):
    return json.loads(git('show', f'{commit}:chromium/pin.json').stdout)['tag']


def release_tags():
    """Every release tag: (name, Chromium tag, revision, commit)."""
    tags = []
    for name in git('tag', '--list', 'v*').stdout.split():
        match = TAG.match(name)
        if match:
            commit = git('rev-list', '-n', '1', name).stdout.strip()
            tags.append((name, match.group(1), int(match.group(2)), commit))
    return tags


def next_revision(tags, chromium):
    revisions = [revision for _, tag, revision, _ in tags if tag == chromium]
    return max(revisions, default=0)+1


def is_strict_descendant(commit, ancestor):
    return commit != ancestor and git('merge-base', '--is-ancestor', ancestor, commit, check=False).returncode == 0


def latest(tags):
    """The release whose commit descends from every other's, or None if there are none."""
    if not tags:
        return None
    return max(tags, key=lambda tag: int(git('rev-list', '--count', tag[3]).stdout))


def for_main(commit='HEAD'):
    if git('rev-parse', '--is-shallow-repository').stdout.strip() != 'false':
        raise SystemExit('the history is shallow: fetch all of it, with the tags, before deciding a version')
    commit = git('rev-parse', commit).stdout.strip()
    tags = release_tags()
    chromium = chromium_tag(commit)
    older = [tag for tag in tags if not is_strict_descendant(commit, tag[3])]
    if older:
        names = ', '.join(sorted(tag[0] for tag in older))
        return {'publish': False, 'reason': f'{commit[:12]} is not newer than every released commit ({names})'}
    version = f'{chromium}-{next_revision(tags, chromium)}'
    previous = latest(tags)
    skipped = []
    if previous:
        # Commits after the previous release and before this one, which a replaced run left without their own.
        log = git('log', '--first-parent', '--format=%H %s', f'{previous[3]}..{commit}^').stdout
        skipped = [line for line in log.splitlines() if line]
    return {'publish': True, 'version': version, 'tag': f'v{version}', 'commit': commit,
            'previous': previous[0] if previous else None, 'unreleased': skipped}


def for_pr(number):
    if not re.fullmatch(r'[1-9]\d*', str(number)):
        raise SystemExit(f'not a pull request number: {number}')
    return f'{chromium_tag()}-pr{number}'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('main')
    pr = sub.add_parser('pr')
    pr.add_argument('number')
    args = parser.parse_args()
    if args.command == 'pr':
        print(for_pr(args.number))
    else:
        print(json.dumps(for_main()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
