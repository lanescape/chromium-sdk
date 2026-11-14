import unittest

from tests.scripts.common import REPO  # noqa: F401
import package

LINUX = {'target_os': 'linux', 'target_cpu': 'x64', 'sysroot': 'build/linux/debian_bullseye_amd64-sysroot'}
MAC = {'target_os': 'mac', 'target_cpu': 'arm64'}


class Flags(unittest.TestCase):
    def test_compile(self):
        mapping = package.path_mapping(LINUX)
        argv = ['../../third_party/llvm-build/Release+Asserts/bin/clang++', '-MMD', '-MF', 'obj/x.o.d',
                '-DCHROMIUM_SDK_EXPORTS', '-D_LIBCPP_ABI_NAMESPACE=__Cr', '-I../..', '-Igen',
                '-I../../third_party/skia', '-Igen/third_party/perfetto', '-isystem',
                '../../third_party/libc++/src/include', '--sysroot=../../build/linux/debian_bullseye_amd64-sysroot',
                '-fno-exceptions', '-std=c++23', '-nostdinc++', '-O2', '-g1', '-Wall', '-Wno-foo', '-Werror',
                '-flto=thin', '-fprofile-use=../../chrome/build/pgo_profiles/x.profdata',
                '-Xclang', '-add-plugin', '-Xclang', 'find-bad-constructs',
                '-Xclang', '-plugin-arg-find-bad-constructs', '-Xclang', 'check-ipc',
                '-Xclang', '-fno-something-abi', '-mllvm', '-instcombine-lower-dbg-declare=0',
                '-fdebug-compilation-dir=.', '-c', '../../chromium_sdk/embedder_probe.cc', '-o', 'obj/x.o']
        flags, defines, includes, system = package.compile_flags(argv, mapping)
        self.assertEqual(defines, ['CHROMIUM_SDK_EXPORTS', '_LIBCPP_ABI_NAMESPACE=__Cr'])
        self.assertEqual(includes, ['{sdk}/include', '{sdk}/gen', '{sdk}/include/third_party/skia',
                                    '{sdk}/gen/third_party/perfetto'])
        self.assertEqual(system, ['{sdk}/include/third_party/libc++/src/include'])
        self.assertEqual(flags, ['--sysroot={sdk}/sysroot', '-fno-exceptions', '-std=c++23', '-nostdinc++',
                                 '-Xclang', '-fno-something-abi'])

    def test_a_build_path_never_ships(self):
        mapping = package.path_mapping(LINUX)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-fsome-file=../../tools/x.txt'], mapping)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-I/home/builder/elsewhere'], mapping)

    def test_mac_sdk_is_the_embedders(self):
        mapping = package.path_mapping(MAC)
        sdk = ('../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/'
               'MacOSX26.2.sdk')
        flags, *_ = package.compile_flags(['clang++', '-isysroot', sdk, '-mmacos-version-min=12.0'], mapping)
        self.assertEqual(flags, ['-isysroot', '{apple_sdk}', '-mmacos-version-min=12.0'])

    def test_link(self):
        mapping = package.path_mapping(LINUX)
        argv = ['python3', '../../build/toolchain/gcc_link_wrapper.py', '--output=./embedder_probe', '--',
                '../../third_party/llvm-build/Release+Asserts/bin/clang++', '-fuse-ld=lld', '-Wl,--icf=all',
                '-flto=thin', '-Wl,--thinlto-jobs=all', '-Wl,-z,now', '-Wl,-z,relro', '-pie',
                '--sysroot=../../build/linux/debian_bullseye_amd64-sysroot',
                '-L../../build/linux/debian_bullseye_amd64-sysroot/usr/lib/x86_64-linux-gnu',
                '-Wl,-rpath=$ORIGIN', '-o', './embedder_probe', '-Wl,--start-group', '@./embedder_probe.rsp',
                './libchromium.so', './libc++.so', '-Wl,--end-group', '-ldl', '-lpthread', '-lrt']
        flags, libraries, system, frameworks = package.link_flags(argv, mapping)
        self.assertEqual(flags, ['-fuse-ld=lld', '-Wl,-z,now', '-Wl,-z,relro', '-pie', '--sysroot={sdk}/sysroot',
                                 '-L{sdk}/sysroot/usr/lib/x86_64-linux-gnu'])
        self.assertEqual(libraries, ['{sdk}/lib/libchromium.so', '{sdk}/lib/libc++.so'])
        self.assertEqual(system, ['dl', 'pthread', 'rt'])
        self.assertEqual(frameworks, [])

    def test_frameworks(self):
        mapping = package.path_mapping(MAC)
        flags, libraries, system, frameworks = package.link_flags(
            ['clang++', '-framework', 'AppKit', '-weak_framework', 'Metal', './libchromium.dylib', '-Wl,-ObjC'],
            mapping)
        self.assertEqual(frameworks, ['AppKit', 'Metal'])
        self.assertEqual(libraries, ['{sdk}/lib/libchromium.dylib'])
        self.assertEqual(flags, ['-Wl,-ObjC'])

    def test_rewrite_path(self):
        mapping = package.path_mapping(LINUX)
        self.assertEqual(package.rewrite_path('../../base/foo.h', mapping), '{sdk}/include/base/foo.h')
        self.assertEqual(package.rewrite_path('gen/base/foo.h', mapping), '{sdk}/gen/base/foo.h')
        self.assertIsNone(package.rewrite_path('/usr/include/stdio.h', mapping))


if __name__ == '__main__':
    unittest.main()
