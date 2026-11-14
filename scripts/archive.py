"""Deterministic .tar.zst archives: the same files always make the same bytes (specs/0001-sdk.md, Parts)."""
import os
import subprocess
import tarfile
from pathlib import Path

# 2000-01-01T00:00:00Z. Every member has this time, so an archive depends only on its files' contents and modes.
EPOCH = 946684800
LEVEL = os.environ.get('CHROMIUM_SDK_ZSTD_LEVEL', '19')


def members(directory):
    """Every path under a directory, relative to it, in a fixed order. Links stay links."""
    directory = Path(directory)
    for root, names, files in os.walk(directory):
        names.sort()
        relative = Path(root).relative_to(directory)
        entries = sorted(files + [name for name in names if (Path(root)/name).is_symlink()])
        if relative != Path('.'):
            yield relative.as_posix()
        for name in entries:
            yield (relative/name).as_posix()
        names[:] = [name for name in names if not (Path(root)/name).is_symlink()]


def normalized(info):
    info.mtime = EPOCH
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    if info.issym():
        info.mode = 0o777
    return info


def write(directory, output, level=None):
    """Archives a directory's contents, with paths relative to it, into a zstd-compressed tar."""
    directory = Path(directory)
    output = Path(output)
    zstd = subprocess.Popen(['zstd', f'-{level or LEVEL}', '-T0', '-q', '-f', '-o', str(output)], stdin=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=zstd.stdin, mode='w|', format=tarfile.PAX_FORMAT) as tar:
            for name in members(directory):
                path = directory/name
                info = normalized(tar.gettarinfo(str(path), arcname=name))
                if info.isfile():
                    with open(path, 'rb') as stream:
                        tar.addfile(info, stream)
                else:
                    tar.addfile(info)
    finally:
        zstd.stdin.close()
        if zstd.wait():
            raise RuntimeError(f'zstd failed writing {output}')
