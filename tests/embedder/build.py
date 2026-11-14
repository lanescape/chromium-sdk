#!/usr/bin/env python3
"""Builds the test embedder against an extracted SDK, outside Chromium's tree, with nothing but the SDK's own
toolchain and the flags in its sdk.json, as any build tool could (specs/0001-sdk.md, Compiling and linking):

    build.py --sdk <extracted SDK> --output <directory> [--apple-sdk <MacOSX.sdk>]

The output directory holds the executable with the SDK's runtime libraries and resources beside it, ready to run
(run.py) or to copy to a machine that tests the target.
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
EXECUTABLE = 'chromium-sdk-test-embedder'


def expand(value, sdk, apple_sdk):
    value = value.replace('{sdk}', str(sdk))
    if '{apple_sdk}' in value:
        if not apple_sdk:
            raise SystemExit('this SDK needs an Apple SDK: pass --apple-sdk')
        value = value.replace('{apple_sdk}', str(apple_sdk))
    return value


def compile_command(description, language, source, output, sdk, apple_sdk):
    flags = [expand(flag, sdk, apple_sdk) for flag in description['compile'][language]]
    defines = ['-D'+define for define in description['defines']]
    includes = ['-I'+expand(path, sdk, apple_sdk) for path in description['include_dirs']]
    system = [part for path in description['system_include_dirs'] for part in ('-isystem', expand(path, sdk,
                                                                                                  apple_sdk))]
    # The embedder's own headers, found from the repository's root as Chromium's are from its.
    return ([expand(description['toolchain']['cxx'], sdk, apple_sdk), '-x', language] + flags + defines +
            ['-I'+str(REPO)] + includes + system + ['-O2', '-c', str(source), '-o', str(output)])


def link_command(description, objects, output, sdk, apple_sdk):
    link = description['link']
    rpath = '-Wl,-rpath,@loader_path' if description['system'] == 'mac' else '-Wl,-rpath,$ORIGIN'
    return ([expand(description['toolchain']['cxx'], sdk, apple_sdk)] +
            [expand(flag, sdk, apple_sdk) for flag in link['flags']] + [str(o) for o in objects] +
            [expand(library, sdk, apple_sdk) for library in link['libraries']] +
            ['-l'+name for name in link['system_libs']] +
            [part for name in link['frameworks'] for part in ('-framework', name)] +
            [rpath, '-o', str(output)])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sdk', required=True, type=Path, help='the directory the SDK parts are extracted to')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--apple-sdk', type=Path, help="macOS: the Apple SDK to build with, the embedder's own")
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    description = json.loads((sdk/'sdk.json').read_text())
    mac = description['system'] == 'mac'
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    objects_dir = output/'obj'
    objects_dir.mkdir(parents=True)
    sources = [(HERE/'embedder.cc', 'c++')]
    sources.append((HERE/'platform_mac.mm', 'objective-c++') if mac else (HERE/'platform_aura.cc', 'c++'))
    commands = []
    objects = []
    for source, language in sources:
        obj = objects_dir/(source.stem+'.o')
        objects.append(obj)
        commands.append(compile_command(description, language, source, obj, sdk, args.apple_sdk))
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda command: subprocess.run(command), commands))
    for command, result in zip(commands, results):
        if result.returncode:
            print('failed:', ' '.join(command), file=sys.stderr)
            return 1
    executable = output/EXECUTABLE
    subprocess.run(link_command(description, objects, executable, sdk, args.apple_sdk), check=True)
    # The runtime files beside the executable, where Chromium looks for them by default.
    for library in description['runtime']['libraries']:
        shutil.copy2(sdk/library, output/Path(library).name)
    for resource in description['runtime']['resources']:
        relative = Path(resource).relative_to('resources')
        (output/relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sdk/resource, output/relative)
    shutil.rmtree(objects_dir)
    print(f'built {executable}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
