"""Deterministic .tar.zst archives: the same files always make the same bytes (specs/0001-sdk.md, Parts)."""
import hashlib
import os
import subprocess
import tarfile
from pathlib import Path

# 2000-01-01T00:00:00Z. Every member has this time, so an archive depends only on its files' contents and modes.
EPOCH = 946684800
# A release's level. Nothing in the environment changes it: CI's quicker packages ask for theirs (package.py).
LEVEL = 19


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


class _Tail:
    """A stream that counts what's read from it and keeps its last bytes, to see how a tar ends."""

    KEEP = 2*tarfile.RECORDSIZE+2*tarfile.BLOCKSIZE

    def __init__(self, stream):
        self.stream, self.length, self.tail = stream, 0, b''

    def read(self, size=-1):
        data = self.stream.read(size)
        self.length += len(data)
        self.tail = (self.tail+data)[-self.KEEP:]
        return data

    def drain(self):
        while self.read(1 << 20):
            pass

    def zeros_after(self, offset):
        """Whether the stream holds at least the two zero blocks that end a tar right after `offset`, and nothing
        but zeros from there to its end."""
        start = offset-(self.length-len(self.tail))
        return (start >= 0 and self.length-offset >= 2*tarfile.BLOCKSIZE and not self.tail[start:].strip(b'\0'))


class Member(tuple):
    """An archive member: its name, its kind ('file', 'dir', 'symlink' or 'other'), its size if a file, and its
    target if a link."""
    __slots__ = ()

    def __new__(cls, name, kind, size=0, link=''):
        return super().__new__(cls, (name, kind, size, link))

    name = property(lambda self: self[0])
    kind = property(lambda self: self[1])
    size = property(lambda self: self[2])
    link = property(lambda self: self[3])


def _kind(member):
    return 'file' if member.isfile() else 'dir' if member.isdir() else 'symlink' if member.issym() else 'other'


def scan(path, wanted=(), digested=()):
    """Reads a .tar.zst archive to its end: every Member, in order, the bytes of each file named in `wanted`, and
    the SHA-256 of each file named in `digested`, each a collection of names or a function that says whether it
    wants one. Fails if zstd or tar can't read all of it, or if the tar doesn't end as one does, with zero blocks
    right after its last member, as when one is cut short between two members."""
    zstd = subprocess.Popen(['zstd', '-dc', '-q', str(path)], stdout=subprocess.PIPE)
    stream = _Tail(zstd.stdout)
    wants = wanted if callable(wanted) else wanted.__contains__
    digests = digested if callable(digested) else digested.__contains__
    members, files, sums, end = [], {}, {}, 0
    try:
        with tarfile.open(fileobj=stream, mode='r|') as tar:
            for member in tar:
                members.append(Member(member.name, _kind(member), member.size if member.isfile() else 0,
                                      member.linkname if member.issym() else ''))
                if member.isfile() and (wants(member.name) or digests(member.name)):
                    reader, digest, kept = tar.extractfile(member), hashlib.sha256(), []
                    while block := reader.read(1 << 20):
                        digest.update(block)
                        if wants(member.name):
                            kept.append(block)
                    if wants(member.name):
                        files[member.name] = b''.join(kept)
                    if digests(member.name):
                        sums[member.name] = digest.hexdigest()
                end = member.offset_data+-(-member.size//tarfile.BLOCKSIZE)*tarfile.BLOCKSIZE \
                    if member.isfile() else member.offset_data
        stream.drain()
    finally:
        zstd.stdout.close()
        if zstd.wait():
            raise RuntimeError(f'zstd failed reading {path}')
    if not stream.zeros_after(end):
        raise tarfile.ReadError(f'{path} does not end as a tar does')
    return members, files, sums


def read(path, name):
    """One file's bytes from a .tar.zst archive, by its path in the archive, or None if the archive has no such
    file."""
    return scan(path, (name,))[1].get(name)


def listing_digest(entries):
    """The SHA-256 of a tree's listing, (path, kind, size if a file, target if a link) for each entry, in any order:
    the same for a tree on disk and an archive of it, whatever their order."""
    lines = sorted(f'{name}\t{kind}\t{size if kind == "file" else ""}\t{link}' for name, kind, size, link in entries)
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def write(directory, output, level=None):
    """Archives a directory's contents, with paths relative to it, into a zstd-compressed tar."""
    directory = Path(directory)
    output = Path(output)
    zstd = subprocess.Popen(['zstd', f'-{level or LEVEL}', '-T0', '-q', '-f', '-o', str(output)],
                            stdin=subprocess.PIPE)
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
