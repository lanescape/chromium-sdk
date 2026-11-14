import unittest

from tests.scripts.common import REPO  # noqa: F401
import export_patch


class RewriteHeader(unittest.TestCase):
    def test_export_header(self):
        text = ('#if defined(COMPONENT_BUILD)\n#if defined(FOO_IMPLEMENTATION)\n'
                '#define FOO_EXPORT __attribute__((visibility("default")))\n#endif\n#else  // defined(COMPONENT_BUILD)\n'
                '#define FOO_EXPORT\n#endif  // defined(COMPONENT_BUILD)\n')
        out = export_patch.rewrite_header(text)
        self.assertTrue(out.startswith('#if (defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS))\n'))
        # Comments after #else and #endif stay as they were.
        self.assertIn('#else  // defined(COMPONENT_BUILD)\n', out)
        self.assertIn('#endif  // defined(COMPONENT_BUILD)\n', out)

    def test_negated_and_elif(self):
        self.assertEqual(export_patch.rewrite_header('#if !defined(COMPONENT_BUILD)\n'),
                         '#if !(defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#elif defined(COMPONENT_BUILD) && defined(WIN32)\n'),
                         '#elif (defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS)) && defined(WIN32)\n')

    def test_partition_alloc(self):
        self.assertEqual(export_patch.rewrite_header('#if PA_BUILDFLAG(IS_COMPONENT_BUILD)\n'),
                         '#if (PA_BUILDFLAG(IS_COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#endif  // PA_BUILDFLAG(IS_COMPONENT_BUILD)\n'),
                         '#endif  // PA_BUILDFLAG(IS_COMPONENT_BUILD)\n')

    def test_ifdef(self):
        self.assertEqual(export_patch.rewrite_header('#ifdef COMPONENT_BUILD\n'),
                         '#if (defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS))\n')
        self.assertEqual(export_patch.rewrite_header('#ifndef COMPONENT_BUILD // x\n'),
                         '#if !(defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS)) // x\n')

    def test_continued_condition(self):
        text = '#if defined(A) && \\\n    !defined(COMPONENT_BUILD) && \\\n    defined(B)\nint defined(COMPONENT_BUILD);\n'
        out = export_patch.rewrite_header(text)
        self.assertIn('    !(defined(COMPONENT_BUILD) || defined(CHROMIUM_SDK_EXPORTS)) && \\\n', out)
        # Code, not a condition, is left alone.
        self.assertTrue(out.endswith('int defined(COMPONENT_BUILD);\n'))

    def test_export_header_detection(self):
        self.assertTrue(export_patch.is_export_header('#if defined(COMPONENT_BUILD)\n__attribute__((visibility("default")))'))
        self.assertFalse(export_patch.is_export_header('#if defined(COMPONENT_BUILD)\nint x;'))
        self.assertFalse(export_patch.is_export_header('__attribute__((visibility("default")))'))

    def test_skipped_trees(self):
        for path in ('chrome/common/chrome_export.h', 'base/test/test_export.h', 'ui/base/foo_win.h',
                     'third_party/pdfium/public/fpdfview.h'):
            self.assertTrue(export_patch.SKIPPED.search(path), path)
        for path in ('content/common/content_export.h', 'base/base_export.h', 'ui/gfx/gfx_export.h'):
            self.assertFalse(export_patch.SKIPPED.search(path), path)

    def test_gn_edit_must_match_once(self):
        self.assertEqual(export_patch.apply_gn_edit('a b c', 'b', 'x', 'f'), 'a x c')
        with self.assertRaises(SystemExit):
            export_patch.apply_gn_edit('a b b', 'b', 'x', 'f')
        with self.assertRaises(SystemExit):
            export_patch.apply_gn_edit('a c', 'b', 'x', 'f')

    def test_owner(self):
        repos = ['', 'v8', 'third_party/perfetto']
        self.assertEqual(export_patch.owner('v8/include/v8config.h', repos), 'v8')
        self.assertEqual(export_patch.owner('third_party/perfetto/gn/BUILD.gn', repos), 'third_party/perfetto')
        self.assertEqual(export_patch.owner('v8x/foo.h', repos), '')


class CarriedPatches(unittest.TestCase):
    def test_patches_only_add_the_sdk_switch(self):
        """Every line the carried patches add mentions the SDK's switch, or is a comment or GN syntax around it."""
        for patch in sorted((REPO/'chromium/patches').glob('**/*.patch')):
            for line in patch.read_text().splitlines():
                if not line.startswith('+') or line.startswith('+++'):
                    continue
                body = line[1:].strip()
                if not body or body.startswith(('#', '//')) and 'COMPONENT_BUILD' not in body:
                    continue
                if body in ('}', 'defines += [ "CHROMIUM_SDK_EXPORTS" ]', 'chromium_sdk_exports = false',
                            'cflags += [ "-fwhole-program-vtables" ]', 'defines += [ "SKIA_DLL" ]',
                            'defines += [ "ABSL_BUILD_DLL" ]', 'perfetto_component_build = true',
                            'if ((defined(is_component_build) && is_component_build) ||',
                            'public_deps += [ "//v8:run_mksnapshot_default" ]',
                            'defines += [ "V8_TLS_USED_IN_LIBRARY" ]',
                            'if ((v8_monolithic && v8_monolithic_for_shared_library) ||',
                            'ldflags = [ "-Wl,-install_name,@rpath/libc++_chrome.dylib" ]',
                            '_component_mode = "source_set"'):
                    continue
                self.assertTrue('CHROMIUM_SDK_EXPORTS' in body or 'chromium_sdk_exports' in body,
                                f'{patch.name}: {line}')


if __name__ == '__main__':
    unittest.main()
