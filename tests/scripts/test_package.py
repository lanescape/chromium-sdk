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
                '-isystem../../third_party/libc++abi/src/include', '-fmodules', '-fno-implicit-module-maps',
                '-Xclang', '-fmodules-local-submodule-visibility', '-fmodule-map-file=gen/build/modules/module.modulemap',
                '-flto=thin', '-fprofile-use=../../chrome/build/pgo_profiles/x.profdata',
                '-Xclang', '-add-plugin', '-Xclang', 'find-bad-constructs',
                '-Xclang', '-plugin-arg-find-bad-constructs', '-Xclang', 'check-ipc',
                '-Xclang', '-fno-something-abi', '-mllvm', '-instcombine-lower-dbg-declare=0',
                '-Xclang', '-fmodule-file-home-is-cwd', '-Xclang', '-fmodules-cache-path=/not_exist_dummy_dir',
                '-fdebug-compilation-dir=.', '-c', '../../chromium_sdk/embedder_probe.cc', '-o', 'obj/x.o']
        flags, defines, includes, system = package.compile_flags(argv, mapping)
        self.assertEqual(defines, ['CHROMIUM_SDK_EXPORTS', '_LIBCPP_ABI_NAMESPACE=__Cr'])
        self.assertEqual(includes, ['{sdk}/include', '{sdk}/gen', '{sdk}/include/third_party/skia',
                                    '{sdk}/gen/third_party/perfetto'])
        self.assertEqual(system, ['{sdk}/include/third_party/libc++/src/include',
                                  '{sdk}/include/third_party/libc++abi/src/include'])
        self.assertEqual(flags, ['--sysroot={sdk}/sysroot', '-fno-exceptions', '-std=c++23', '-nostdinc++',
                                 '-Xclang', '-fno-something-abi'])

    def test_a_build_path_never_ships(self):
        mapping = package.path_mapping(LINUX)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-fsome-file=../../tools/x.txt'], mapping)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-I/home/builder/elsewhere'], mapping)
        with self.assertRaises(package.PathLeak):
            package.compile_flags(['clang++', '-Xclang', '-fsome-file=/home/builder/x'], mapping)

    def test_mac_sdk_is_the_embedders(self):
        mapping = package.path_mapping(MAC)
        sdk = ('../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/'
               'MacOSX26.2.sdk')
        flags, *_ = package.compile_flags(['clang++', '-isysroot', sdk, '-mmacos-version-min=12.0'], mapping)
        self.assertEqual(flags, ['-isysroot', '{apple_sdk}', '-mmacos-version-min=12.0'])

    def test_link(self):
        mapping = package.path_mapping(LINUX)
        variables = {
            'ldflags': ['-Wl,-mllvm,-enable-ext-tsp-block-placement=1', '-Werror', '-fPIC', '-flto=thin',
                        '-Wl,--thinlto-cache-dir=thinlto-cache', '-Wl,--icf=all', '--target=x86_64-unknown-linux-gnu',
                        '-Wl,-O2', '-Wl,--gc-sections', '-gsplit-dwarf', '-Wl,-z,defs', '-Wl,-z,now', '-nostdlib++',
                        '--sysroot=../../build/linux/debian_bullseye_amd64-sysroot',
                        '-L../../build/linux/debian_bullseye_amd64-sysroot/usr/lib/x86_64-linux-gnu',
                        '-Wl,-rpath=$ORIGIN', '-pie'],
            'libs': ['../../third_party/llvm-build/Release+Asserts/lib/clang/24/lib/x86_64-unknown-linux-gnu/'
                     'libclang_rt.builtins.a', '-ldl', '-lpthread'],
            'frameworks': [],
            'solibs': ['./libchromium.so', './libc++.so'],
        }
        flags, libraries, system, frameworks = package.link_flags(variables, mapping)
        self.assertEqual(flags, ['-fPIC', '--target=x86_64-unknown-linux-gnu', '-Wl,--gc-sections', '-Wl,-z,defs',
                                 '-Wl,-z,now', '-nostdlib++', '--sysroot={sdk}/sysroot',
                                 '-L{sdk}/sysroot/usr/lib/x86_64-linux-gnu', '-pie'])
        self.assertEqual(libraries, ['{sdk}/toolchain/lib/clang/24/lib/x86_64-unknown-linux-gnu/libclang_rt.builtins.a',
                                     '{sdk}/lib/libchromium.so', '{sdk}/lib/libc++.so'])
        self.assertEqual(system, ['dl', 'pthread'])
        self.assertEqual(frameworks, [])

    def test_mac_link(self):
        mapping = package.path_mapping(MAC)
        variables = {
            'ldflags': ['-Wl,--no-warn-duplicate-rpath', '-Wcrl,object_path_lto',
                        '-Wcrl,unstripped,/home/builder/checkout/src/out/macos-arm64', '--target=arm64-apple-macos',
                        '-Wl,-ObjC', '-isysroot', '../../build/mac_files/xcode_binaries/Contents/Developer/Platforms/'
                        'MacOSX.platform/Developer/SDKs/MacOSX26.2.sdk', '-mmacos-version-min=12.0'],
            'libs': ['../../third_party/llvm-build/Release+Asserts/lib/clang/24/lib/darwin/libclang_rt.osx.a'],
            'frameworks': ['-framework', 'AppKit', '-weak_framework', 'Metal'],
            'solibs': ['./libchromium.dylib', './libc++_chrome.dylib'],
        }
        flags, libraries, system, frameworks = package.link_flags(variables, mapping)
        self.assertEqual(flags, ['-Wl,--no-warn-duplicate-rpath', '--target=arm64-apple-macos', '-Wl,-ObjC',
                                 '-isysroot', '{apple_sdk}', '-mmacos-version-min=12.0'])
        self.assertEqual(frameworks, ['AppKit', 'Metal'])
        self.assertEqual(libraries[1:], ['{sdk}/lib/libchromium.dylib', '{sdk}/lib/libc++_chrome.dylib'])

    def test_a_link_path_never_ships(self):
        with self.assertRaises(package.PathLeak):
            package.link_flags({'ldflags': ['-Wl,--version-script=/home/builder/x.map']}, package.path_mapping(LINUX))
        with self.assertRaises(package.PathLeak):
            package.link_flags({'solibs': ['obj/libx.so']}, package.path_mapping(LINUX))

    def test_ninja_variables(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            ninja = Path(tmp)/'probe.ninja'
            ninja.write_text('defines = -DA -DB=\\"x\\"\ninclude_dirs = -I../.. -Igen\n'
                             'cflags_cc = -std=c++23 -isystem../../third_party/libc++/src/include\n'
                             'build obj/a.o: cxx ../../a.cc\n  source_file_part = a.cc\n'
                             'build ./probe: link obj/a.o\n  ldflags = -Wl,--thinlto-cache-policy=cache_size=10\\%$:x\n'
                             '  libs = -ldl\n  solibs = ./libchromium.so\n')
            variables = package.target_variables(ninja)
        self.assertEqual(variables['defines'], ['-DA', '-DB="x"'])
        self.assertEqual(variables['ldflags'], ['-Wl,--thinlto-cache-policy=cache_size=10%:x'])
        self.assertEqual(variables['solibs'], ['./libchromium.so'])
        argv = package.compile_argv(variables, 'c++')
        self.assertEqual(argv, ['clang++', '-DA', '-DB="x"', '-I../..', '-Igen', '-std=c++23',
                                '-isystem../../third_party/libc++/src/include'])

    def test_runtime_files(self):
        for name in ('./libffmpeg.so', './libEGL.so', './libGLESv2.dylib', './libvk_swiftshader.so',
                     './libvulkan.so.1', './libvulkan.dylib', './vk_swiftshader_icd.json'):
            self.assertTrue(package.RUNTIME.match(name), name)
        for name in ('./libVkICD_mock_icd.so', './libVkLayer_khronos_validation.so', 'angledata/VkICD_mock_icd.json',
                     'pyproto/google/protobuf/any.py', './libchromium.so'):
            self.assertFalse(package.RUNTIME.match(name), name)

    def test_shipped_runtime_listing(self):
        listing = ('./libchromium.so\nicudtl.dat\n./libEGL.so\n./libc++.so\nvk_swiftshader_icd.json\n'
                   './libvk_swiftshader.so\n./libvulkan.so.1\n./libVkICD_mock_icd.so\nangledata/VkICD_mock_icd.json\n'
                   './libffmpeg.so\nsnapshot_blob.bin\npyproto/google/protobuf/any.py\n')
        self.assertEqual(package.shipped_runtime(listing),
                         ['libEGL.so', 'libffmpeg.so', 'libvk_swiftshader.so', 'libvulkan.so.1',
                          'vk_swiftshader_icd.json'])

    def test_rewrite_path(self):
        mapping = package.path_mapping(LINUX)
        self.assertEqual(package.rewrite_path('../../base/foo.h', mapping), '{sdk}/include/base/foo.h')
        self.assertEqual(package.rewrite_path('gen/base/foo.h', mapping), '{sdk}/gen/base/foo.h')
        self.assertIsNone(package.rewrite_path('/usr/include/stdio.h', mapping))


if __name__ == '__main__':
    unittest.main()
