# Licences

This repository's own files are under the MIT License (`LICENSE`). What it publishes is Chromium's, under Chromium's licences, and a version that can't meet every obligation below isn't published: a failure in any step that makes notices or source fails CD before it publishes.

## Notices

Every target's `notices/` directory, in the `lib` part and as the `notices` part, holds:

- `LICENSE.chromium`, Chromium's licence;
- `CREDITS.html`, the licences of every third-party component the SDK's targets link or ship, made by Chromium's `tools/licenses/licenses.py credits` over `//libchromium:libchromium`;
- `LICENSE.libchromium`, this repository's licence;
- `components.json`, each of those components, its licence, its directory, and whether its corresponding source is published with the version (`copyleft`);
- `gaps/`, the licence texts of the components Chromium's metadata can't credit at the pin, which `chromium/licence-gaps.json` lists, each with its licence, the files whose texts ship and why. Packaging fails if Chromium's metadata has any error that list doesn't explain, so a new gap is reviewed before anything ships.

The toolchain is LLVM's, under the Apache License 2.0 with LLVM Exceptions, whose text ships as `toolchain/LICENSE.TXT`. The sysroots' packages' copyright files are in their Debian source, below.

## Corresponding source

The release that holds a version's binaries also holds the corresponding source of every component whose licence asks for it, as `source-<name>` assets:

- **Components of the build.** Every component in `components.json` whose licence is a copyleft one (LGPL, GPL, MPL, EPL, CDDL and their like) is archived from the checkout as built, with the SDK's patch applied, and without directories the build never reads, such as Blink's web tests. A Rust crate's archive holds Chromium's directory for it and the vendored source its build names. For the first pin, that is FFmpeg (LGPL 2.1), Blink's WebKit-derived code in `third_party/blink` (LGPL 2 and 2.1), the GTK and Speech Dispatcher headers (LGPL), plasma-wayland-protocols (LGPL 2.1) and the `symphonia` Rust crates (MPL 2.0). The list comes from Chromium's metadata at every build, never from this document, so a new pin's components are covered as they appear.
- **The sysroots.** A Linux SDK ships Chromium's Debian sysroot, which is Debian's packages, among them the GNU C Library (LGPL), GLib (LGPL) and NSS (MPL). `source-debian-bullseye-sysroots` holds the Debian source package (`.dsc` and the files it lists, verified against it) of every package in either Linux sysroot, at the versions in them, from `snapshot.debian.org`'s archive of the date the sysroot was made, and `packages.json`, which lists them.
- **This repository**, at the commit that built the version, which holds the settings, the patch and the scripts: `source-libchromium`.

Chromium's own source at the pin, which is BSD-licensed, is public at `chromium.googlesource.com`, and with the patch and scripts above rebuilds the version.

## FFmpeg

FFmpeg is a separate shared library, `libffmpeg`, that the library loads at runtime. LGPL 2.1 lets a user run the SDK's library with an FFmpeg of their own: they can build `libffmpeg` from its published source with Chromium's build files at the pin and the version's `args.gn`, and put it in the shipped one's place. Proprietary codecs are off.

## Apple's SDK

Apple's SDK is used only on build machines, from the organization's private bucket for CI tools, under Apple's terms. It is never packaged, published or redistributed, in any asset, artifact or log. A macOS embedder builds with its own.
