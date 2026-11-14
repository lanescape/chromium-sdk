#!/usr/bin/env python3
"""Runs a built test embedder (build.py) and fails unless it reports the page's result: run.py <directory>.

Chromium's child processes go with it when it exits or runs out of time, so nothing outlives the test.
"""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

from build import EXECUTABLE

TIMEOUT = 180


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('directory', type=Path)
    parser.add_argument('arguments', nargs='*', help="extra switches for Chromium, after --")
    args = parser.parse_args()
    executable = args.directory.resolve()/EXECUTABLE
    command = [str(executable), '--enable-logging=stderr', *args.arguments]
    print('+', ' '.join(command), flush=True)
    process = subprocess.Popen(command, start_new_session=True)
    try:
        code = process.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f'no exit after {TIMEOUT} s', flush=True)
        code = 124
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
    print(f'test embedder exited {code}', flush=True)
    return 0 if code == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
