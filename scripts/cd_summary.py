#!/usr/bin/env python3
"""Turns version.py main's answer, on stdin, into CD's step outputs and job summary: whether this push publishes,
its version, and any commits since the previous release that weren't released on their own."""
import json
import os
import sys


def lines(answer):
    if not answer['publish']:
        return [f'Publishes nothing: {answer["reason"]}.']
    out = [f'Publishes {answer["version"]} (tag {answer["tag"]}) from {answer["commit"][:12]}.']
    if answer['unreleased']:
        out.append(f'Commits since {answer["previous"]} without a release of their own, which this one includes:')
        out += [f'- {line}' for line in answer['unreleased']]
    return out


def main():
    answer = json.load(sys.stdin)
    text = '\n'.join(lines(answer))
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(text+'\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'publish={"true" if answer["publish"] else "false"}\n')
            output.write(f'version={answer.get("version", "")}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
