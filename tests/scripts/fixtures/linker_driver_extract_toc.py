# Copyright 2016 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# LinkerDriver._extract_toc() and the function after it, from Chromium's build/toolchain/apple/linker_driver.py
# at the pin, unchanged, for tests/scripts/test_export_patch.py, which applies the patch's edits and runs both.
import subprocess


class LinkerDriver(object):
    def _extract_toc(self):
        """Extract TOC from linker output.

        Returns:
           output contents in bytes.
        """
        toc = b''
        dylib = self._get_linker_output()
        out = subprocess.check_output(self._otool_cmd + ['-l', dylib])
        lines = out.split(b'\n')
        found_id = False
        for i, line in enumerate(lines):
            # Too many LC_ID_DYLIBs? We didn’t understand something about
            # the otool output. Raise an exception and die, rather than
            # proceeding.

            # Not any LC_ID_DYLIBs? Probably not an MH_DYLIB. Probably fine, we
            # can proceed with ID-less TOC generation.
            if line == b'      cmd LC_ID_DYLIB':
                if found_id:
                    raise ValueError('Too many LC_ID_DYLIBs in %s' % dylib)
                toc += line + b'\n'
                for j in range(5):
                    toc += lines[i + 1 + j] + b'\n'
                found_id = True

        # -U ignores undefined symbols
        # -g display only global (external) symbols
        # -p unsorted https://crrev.com/c/2173969
        out = subprocess.check_output(self._nm_cmd + ['-Ugp', dylib])
        lines = out.split(b'\n')
        for line in lines:
            fields = line.split(b' ', 2)
            if len(fields) < 3:
                continue
            # fields = (value, type, name)
            # emit [type, name]
            toc += b' '.join(fields[1:3]) + b'\n'
        return toc


def _find_tools_paths(full_args):
    """Finds all paths where the script should look for additional tools."""
    paths = []
    for idx, arg in enumerate(full_args):
        if arg in ['-B', '--prefix']:
            paths.append(full_args[idx + 1])
        elif arg.startswith('-B'):
            paths.append(arg[2:])
        elif arg.startswith('--prefix='):
            paths.append(arg[9:])
    return paths
