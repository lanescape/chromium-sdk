"""Packages a built target as the parts of an SDK version (specs/0001-sdk.md, Parts; specs/0002-build.md,
Packaging). sdk.py package calls package(); the flag and header logic is here so tests can reach it."""
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import archive
import licences

REPO = Path(__file__).resolve().parent.parent
SCHEMA = 1
LIMIT = 2*1024**3

# Root headers: the headers of every target linked into the library whose directory is one of these. Their
# closure under the SDK's flags is what ships (headers()).
HEADER_ROOTS = (
    '//base', '//build', '//cc', '//components/embedder_support', '//components/input',
    '//components/keyed_service', '//components/viz/common', '//components/viz/host', '//content/public', '//gin',
    '//gpu', '//ipc', '//media', '//mojo/public', '//net', '//sandbox', '//skia', '//storage/browser',
    '//storage/common', '//third_party/blink/public', '//ui', '//url', '//v8/include',
)
SERVICE_ROOT = re.compile(r'^//services/[^/:]+/public(/|:|$)')

# Compile flags that are about Chromium's own build rather than what its headers need: diagnostics, warnings,
# optimisation, debug information, profiles, LTO and Chromium's style plugins. An embedder picks its own.
DROP_EXACT = {'-c', '-MMD', '-MD', '-Werror', '-w', '-fcolor-diagnostics', '-fno-color-diagnostics',
              '-fwhole-program-vtables', '-fsplit-lto-unit', '-fno-split-lto-unit', '-nostdinc++x'}
DROP_PREFIX = ('-W', '-O', '-g', '-flto', '-fprofile-', '-fdebug-', '-ffile-compilation-dir', '-fdebug-prefix-map',
               '-fmacro-prefix-map', '-ffile-prefix-map', '-fcrash-diagnostics', '-fdiagnostics-',
               '-fsanitize-ignorelist', '-fsanitize-blacklist', '-fcoverage', '--warning-suppression-mappings',
               '-fno-crash-diagnostics', '-fmessage-length', '-ftime-trace', '-fbasic-block-sections',
               '-fdata-sections', '-ffunction-sections', '-funique-section-names', '-fno-unique-section-names',
               '-fexperimental-relative-c++-abi-vtables=0')
DROP_WITH_VALUE = {'-o', '-MF', '-MT', '-MQ', '-x'}
XCLANG_DROP = ('-add-plugin', '-load', '-plugin-arg-', '-debug-info-kind', '-fuse-ctor-homing',
               '-fdebug-compilation-dir', '-fno-pch-timestamp')
# Link flags that only tune or diagnose Chromium's own links, or choose where the binary finds its libraries,
# which is the embedder's call.
LINK_DROP_PREFIX = ('-flto', '-Wl,--thinlto', '-Wl,--lto', '-Wl,-mllvm', '-Wl,-plugin-opt', '-Wl,--icf',
                    '-Wl,--color-diagnostics', '--color-diagnostics', '-fprofile-', '-Wl,--gdb-index', '-Werror',
                    '-Wl,-rpath', '-Wl,--no-call-graph-profile-sort', '-Wl,-cache_path_lto', '-Wl,--thinlto-cache',
                    '-Wl,-prune_after_lto', '-Wl,-no_warn', '-Wl,--fatal-warnings', '-Wl,-fatal_warnings',
                    '-Wl,--warn-', '-Wl,--lto-O', '-Wl,-cache_policy', '-fwhole-program-vtables',
                    '-Wl,--build-id', '-Wl,--undefined-version', '-Wl,-mcpu', '-Wl,--save-temps')


class PathLeak(Exception):
    """A flag would carry a build machine's path into the SDK."""


def rewrite_path(path, mapping):
    """An in-tree path, relative to the build directory, as {sdk}/..., or None if it isn't one the SDK ships."""
    normal = os.path.normpath(path)
    for prefix, replacement in mapping:
        if normal == prefix:
            return replacement
        if normal.startswith(prefix+'/'):
            return replacement+normal[len(prefix):]
    return None


def path_mapping(target_entry):
    """Prefixes of paths in the build's commands, relative to the build directory, longest first."""
    pairs = [('gen', '{sdk}/gen'), ('../..', '{sdk}/include'), ('.', '{sdk}/lib')]
    if target_entry.get('sysroot'):
        pairs.append((f'../../{target_entry["sysroot"]}', '{sdk}/sysroot'))
    pairs.append(('../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs',
                  '{apple_sdk}/..'))
    pairs.append(('../../third_party/llvm-build/Release+Asserts', '{sdk}/toolchain'))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def mapped(path, mapping):
    result = rewrite_path(path, mapping)
    if result is None:
        raise PathLeak(path)
    if result.startswith('{apple_sdk}/../'):
        # Any SDK version: the embedder's own.
        result = '{apple_sdk}'
    return result


def compile_flags(argv, mapping):
    """The flags an embedder compiles with, from one of Chromium's compile commands, with their paths made
    relative to the SDK. Returns (flags, defines, include_dirs, system_include_dirs)."""
    flags, defines, includes, system_includes = [], [], [], []
    args = list(argv[1:])
    i = 0
    drop_next_xclang = False
    while i < len(args):
        arg = args[i]
        nxt = args[i+1] if i+1 < len(args) else None
        if arg == '-Xclang':
            if drop_next_xclang:
                drop_next_xclang = False
            elif nxt.startswith(XCLANG_DROP):
                drop_next_xclang = nxt in ('-add-plugin', '-load') or nxt.startswith('-plugin-arg-')
            else:
                flags += [arg, nxt]
            i += 2
            continue
        if arg == '-mllvm':
            i += 2
            continue
        if arg in DROP_WITH_VALUE:
            i += 2
            continue
        if arg in DROP_EXACT or arg.startswith(DROP_PREFIX) or arg.endswith(('.cc', '.c', '.mm', '.m', '.cpp')):
            i += 1
            continue
        if arg.startswith('-D'):
            defines.append(arg[2:])
        elif arg.startswith('-I'):
            value = arg[2:] or nxt
            includes.append(mapped(value, mapping))
            i += 0 if arg[2:] else 1
        elif arg in ('-isystem', '-idirafter', '-iquote'):
            (system_includes if arg != '-iquote' else includes).append(mapped(nxt, mapping))
            i += 1
        elif arg.startswith('--sysroot='):
            flags.append('--sysroot='+mapped(arg[len('--sysroot='):], mapping))
        elif arg == '-isysroot':
            flags += ['-isysroot', mapped(nxt, mapping)]
            i += 1
        elif '../' in arg or arg.startswith(('gen/', 'obj/', './')):
            raise PathLeak(arg)
        else:
            flags.append(arg)
        i += 1
    return flags, defines, includes, system_includes


def link_flags(argv, mapping):
    """The flags and inputs an embedder links with, from the probe's link command. Returns (flags, libraries,
    system_libs, frameworks): libraries are SDK files, system_libs names for -l."""
    args = list(argv)
    if '--' in args:
        args = args[args.index('--')+1:]
    args = args[1:]  # the compiler driver, which the toolchain names
    flags, libraries, system_libs, frameworks = [], [], [], []
    i = 0
    while i < len(args):
        arg = args[i]
        nxt = args[i+1] if i+1 < len(args) else None
        if arg == '-o':
            i += 2
            continue
        if arg.startswith('@') or arg.endswith('.o') or arg in ('-Wl,--start-group', '-Wl,--end-group'):
            i += 1
            continue
        if arg.startswith(LINK_DROP_PREFIX) or re.fullmatch(r'-Wl,-O\d', arg):
            i += 1
            continue
        if arg == '-framework' or arg == '-weak_framework':
            frameworks.append(nxt)
            i += 2
            continue
        if arg.startswith('-l'):
            system_libs.append(arg[2:])
        elif re.match(r'^\./lib[^/]+\.(so|dylib)$', arg):
            libraries.append('{sdk}/lib/'+arg[2:])
        elif arg.startswith('--sysroot='):
            flags.append('--sysroot='+mapped(arg[len('--sysroot='):], mapping))
        elif arg == '-isysroot':
            flags += ['-isysroot', mapped(nxt, mapping)]
            i += 1
        elif arg.startswith('-L'):
            flags.append('-L'+mapped(arg[2:], mapping))
        elif arg.startswith('-Wl,-rpath-link='):
            flags.append('-Wl,-rpath-link='+mapped(arg[len('-Wl,-rpath-link='):], mapping))
        elif '../' in arg or arg.startswith(('gen/', 'obj/', './')):
            raise PathLeak(arg)
        else:
            flags.append(arg)
        i += 1
    return flags, libraries, system_libs, frameworks


def ninja_command(out, output):
    """The command ninja runs to make one output of the build."""
    ninja = Path(out).parent.parent/'third_party/ninja/ninja'
    text = subprocess.check_output([str(ninja), '-C', str(out), '-t', 'commands', '-s', output], text=True)
    return shlex.split(text.strip().splitlines()[-1])


def gn_json(src, out, *args):
    gn = shutil.which('gn') or str(Path(src).parent.parent/'depot_tools/gn')
    return json.loads(subprocess.check_output([gn, 'desc', '--format=json', str(out), *args], cwd=src, text=True))


def root_headers(src, out):
    """The headers of the library's public targets, as paths relative to the build directory."""
    linked = set(gn_json(src, out, '//chromium_sdk:chromium', 'deps', '--all')['//chromium_sdk:chromium']['deps'])
    described = {}
    for field in ('sources', 'public'):
        for label, fields in gn_json(src, out, '//*', field).items():
            described.setdefault(label, {}).update(fields)
    roots = set()
    for label, fields in described.items():
        if label not in linked:
            continue
        directory = label.split(':')[0]
        if not (any(directory == root or directory.startswith(root+'/') for root in HEADER_ROOTS)
                or SERVICE_ROOT.match(directory)):
            continue
        listed = [value for key in ('sources', 'public') for value in fields.get(key, [])
                  if isinstance(fields.get(key), list)]
        for path in listed:
            if path.endswith('.h') and not re.search(r'(_win|_android|_ios|_fuchsia|_chromeos)\.h$', path):
                roots.add(path)
    for header in (Path(src)/'v8/include').glob('**/*.h'):
        roots.add('//'+header.relative_to(src).as_posix())
    out_label = '//'+Path(out).relative_to(src).as_posix()+'/'
    return sorted('gen/'+path[len(out_label)+4:] if path.startswith(out_label+'gen/')
                  else '../../'+path[2:] for path in roots)


def closure(compile_argv, out, roots, language, jobs):
    """Every file the roots include, relative to the build directory, and the roots that don't preprocess alone
    with the SDK's flags, such as another platform's."""
    base = [arg for arg in compile_argv if not arg.endswith(('.cc', '.mm'))]
    cleaned = []
    skip = False
    for arg in base:
        if skip:
            skip = False
            continue
        if arg in ('-o', '-MF', '-c'):
            skip = arg != '-c'
            continue
        if arg == '-MMD':
            continue
        cleaned.append(arg)

    def one(root):
        result = subprocess.run(cleaned+['-x', language, '-M', '-w', root], cwd=out, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if result.returncode:
            return root, None
        text = result.stdout.replace('\\\n', ' ')
        return root, text.split(':', 1)[1].split()

    found, failed = set(), []
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for root, deps in pool.map(one, roots):
            if deps is None:
                failed.append(root)
            else:
                found.update(os.path.normpath(dep) for dep in deps)
    return found, failed


def strip(toolchain, path, system):
    if system == 'mac':
        # Without local symbols. llvm-strip signs ad hoc again what it strips.
        subprocess.run([str(toolchain/'bin/llvm-strip'), '-x', str(path)], check=True)
    else:
        subprocess.run([str(toolchain/'bin/llvm-strip'), '--strip-all', str(path)], check=True)


def package(target, checkout, out, version, output):
    from sdk import target_entry, pin, summary  # noqa: avoids a cycle at import
    entry = target_entry(target)
    system = entry['target_os']
    src = checkout/'src'
    out = Path(out)
    toolchain_src = src/'third_party/llvm-build/Release+Asserts'
    output.mkdir(parents=True, exist_ok=True)
    stage = output/'stage'/target
    if stage.exists():
        shutil.rmtree(stage)
    mapping = path_mapping(entry)
    suffix = '.dylib' if system == 'mac' else '.so'

    # Flags, from the probe's own commands.
    compile_cc = ninja_command(out, 'obj/chromium_sdk/embedder_probe/embedder_probe.o')
    flags_cc, defines, includes, system_includes = compile_flags(compile_cc, mapping)
    languages = {'c++': flags_cc}
    if system == 'mac':
        compile_mm = ninja_command(out, 'obj/chromium_sdk/embedder_probe/embedder_probe_mac.o')
        languages['objective-c++'] = compile_flags(compile_mm, mapping)[0]
    link = ninja_command(out, './embedder_probe')
    ldflags, libraries, system_libs, frameworks = link_flags(link, mapping)

    # Headers: the closure of the public targets' headers under those flags.
    roots = root_headers(src, out)
    files, failed = closure(compile_cc, out, roots, 'objective-c++' if system == 'mac' else 'c++',
                            os.cpu_count() or 8)
    headers = {}
    for path in sorted(files):
        rewritten = rewrite_path(path, [('gen', 'gen'), ('../..', 'include')])
        if rewritten and not path.startswith(('../../third_party/llvm-build/', f'../../{entry.get("sysroot", "-")}/',
                                              '../../build/mac_files/')):
            headers[rewritten] = out/path
    summary(f'{target}: {len(headers)} headers from {len(roots)-len(failed)} of {len(roots)} root headers')

    parts = {}

    def part(name):
        directory = stage/name
        directory.mkdir(parents=True, exist_ok=True)
        parts[name] = directory
        return directory

    # The library, its runtime files, its settings and notices.
    lib = part('lib')
    (lib/'lib').mkdir()
    runtime_libraries = []
    for name in (f'libchromium{suffix}', f'libc++{suffix}', f'libffmpeg{suffix}'):
        shutil.copy2(out/name, lib/'lib'/name)
        runtime_libraries.append(f'lib/{name}')
    resources = lib/'resources'
    shutil.copytree(out/'chromium_sdk', resources)
    for name in ('icudtl.dat', 'v8_context_snapshot.bin', 'snapshot_blob.bin'):
        if (out/name).exists():
            shutil.copy2(out/name, resources/name)
    shutil.copy2(out/'args.gn', lib/'args.gn')

    # Debug information, then the stripped libraries.
    if system == 'linux':
        symbols = part('symbols')/'symbols'
        symbols.mkdir()
        for relative in runtime_libraries:
            library = lib/relative
            debug = symbols/(library.name+'.debug')
            subprocess.run([str(toolchain_src/'bin/llvm-objcopy'), '--only-keep-debug', str(library), str(debug)],
                           check=True)
            strip(toolchain_src, library, system)
            subprocess.run([str(toolchain_src/'bin/llvm-objcopy'), f'--add-gnu-debuglink={debug}', str(library)],
                           check=True)
    else:
        for relative in runtime_libraries:
            strip(toolchain_src, lib/relative, system)

    headers_dir = part('headers')
    for relative, source in headers.items():
        destination = headers_dir/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    toolchain = part('toolchain')/'toolchain'
    clang_version = copy_toolchain(toolchain_src, toolchain, entry)

    if entry.get('sysroot'):
        shutil.copytree(src/entry['sysroot'], part('sysroot')/'sysroot', symlinks=True)

    notices = part('notices')/'notices'
    licences.notices(src, out, notices)
    shutil.copytree(notices, lib/'notices')

    description = {
        'schema': SCHEMA,
        'version': version,
        'target': target,
        'chromium': pin(),
        'system': system,
        'cpu': entry['target_cpu'],
        'clang': clang_version,
        'toolchain': {
            'cc': '{sdk}/toolchain/bin/clang', 'cxx': '{sdk}/toolchain/bin/clang++',
            'ar': '{sdk}/toolchain/bin/llvm-ar', 'nm': '{sdk}/toolchain/bin/llvm-nm',
            'strip': '{sdk}/toolchain/bin/llvm-strip', 'objcopy': '{sdk}/toolchain/bin/llvm-objcopy',
            'linker': '{sdk}/toolchain/bin/'+('ld64.lld' if system == 'mac' else 'ld.lld'),
        },
        'sysroot': '{sdk}/sysroot' if entry.get('sysroot') else None,
        'defines': defines,
        'include_dirs': includes,
        'system_include_dirs': system_includes,
        'compile': languages,
        'link': {'flags': ldflags, 'libraries': libraries, 'system_libs': system_libs, 'frameworks': frameworks},
        'runtime': {'libraries': runtime_libraries,
                    'resources': sorted(p.relative_to(lib).as_posix() for p in resources.rglob('*') if p.is_file())},
        'unpreprocessed_root_headers': len(failed),
    }
    if system == 'mac':
        description['apple_sdk'] = {'built_with': entry['mac_sdk']['sdk'], 'xcode': entry['mac_sdk']['xcode']}
    (lib/'sdk.json').write_text(json.dumps(description, indent=2)+'\n')

    written = []
    for name, directory in parts.items():
        asset = output/f'chromium-sdk-{version}-{target}-{name}.tar.zst'
        archive.write(directory, asset)
        size = asset.stat().st_size
        written.append({'name': asset.name, 'target': target, 'part': name, 'size': size})
        summary(f'{asset.name}: {size/1024**2:.0f} MiB')
        if size >= LIMIT:
            raise SystemExit(f'{asset.name} is {size} bytes, at or over the 2 GiB limit for a release asset')
    (output/f'assets-{target}.json').write_text(json.dumps(written, indent=2)+'\n')
    shutil.rmtree(output/'stage')


TOOLS = ('clang', 'clang++', 'lld', 'ld.lld', 'ld64.lld', 'llvm-ar', 'llvm-nm', 'llvm-objcopy', 'llvm-objdump',
         'llvm-readelf', 'llvm-readobj', 'llvm-strip', 'llvm-symbolizer', 'llvm-cxxfilt', 'llvm-dwarfdump',
         'llvm-install-name-tool', 'llvm-otool')


def copy_toolchain(source, destination, entry):
    """Chromium's clang, its own headers and the target's runtime libraries; it runs on Linux x86-64."""
    (destination/'bin').mkdir(parents=True)
    for tool in TOOLS:
        path = source/'bin'/tool
        if path.is_symlink():
            os.symlink(os.readlink(path), destination/'bin'/tool)
        elif path.exists():
            shutil.copy2(path, destination/'bin'/tool)
    versions = sorted((source/'lib/clang').iterdir())
    resource = versions[-1]
    target_dir = 'darwin' if entry['target_os'] == 'mac' else entry['clang_target']
    (destination/'lib/clang'/resource.name/'lib').mkdir(parents=True)
    shutil.copytree(resource/'include', destination/'lib/clang'/resource.name/'include')
    shutil.copytree(resource/'lib'/target_dir, destination/'lib/clang'/resource.name/'lib'/target_dir)
    for name in ('cr_build_revision',):
        if (source/name).exists():
            shutil.copy2(source/name, destination/name)
    revision = (source/'cr_build_revision').read_text().strip() if (source/'cr_build_revision').exists() else ''
    licences.toolchain_notice(source.parent.parent.parent, destination)
    return {'version': resource.name, 'revision': revision}
