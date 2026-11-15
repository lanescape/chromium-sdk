# Building the SDK

`scripts/sdk.py <step> <target>` builds one target, on Linux x86-64 for every target. Its steps, in order, are `checkout`, `packages`, `mac-sdk`, `patch`, `configure`, `build`, `package`, and for a release `sources`. Each is safe to run again on a kept disk, such as one the runner service restores or resumes (specs/0003-ci-cd.md): a step that finds its work done does nothing, and a build carries on from what Siso finds. The checkout is at `$LIBCHROMIUM_CHECKOUT` (default `~/libchromium/checkout`) and depot_tools at `$DEPOT_TOOLS` (default beside it).

## Checkout

`chromium/pin.json` names the Chromium release tag and its commit; `checkout` refuses a tag that names another commit. `chromium/gclient` is the checkout's `.gclient`: one checkout serves every target, so it lists `mac` with `linux` and `arm64` with `x64`, which brings macOS's toolchain pieces, Linux's arm64 sysroot and the PGO profiles to a Linux machine. `checkout` fetches the tag, runs `gclient sync` at it with no history, and `gclient runhooks`, and records that it finished for that pin and `.gclient`, so a later run skips it. A checkout that had the patch applied is reverted to the pin first. `packages` installs Chromium's Linux build dependencies with its own `install-build-deps.sh` on every run, since they live on the machine while the checkout lives on the kept disk; it never waits for a password.

## Settings

`chromium/args.gn` is the same for every target; `configure` adds the target's `target_os` and `target_cpu` from `targets.json`, and for macOS the Apple SDK's version and the settings a Linux machine needs (below). It writes the build directory's `args.gn` only when it differs, since a newer one regenerates everything.

There is one configuration per target, Chromium's release build, which embedders' development and release builds both link, so that what an embedder tests is what it ships, and so that the SDK's flags, which fix `DCHECK_IS_ON()` and every build flag in the headers, are one set:

- `is_official_build = true`, `is_debug = false`, `dcheck_always_on = false`, with PGO and ThinLTO as official builds have them; `symbol_level = 1`, `blink_symbol_level = 0`, `v8_symbol_level = 0`.
- `is_component_build = false`: one library, which the patch's `libchromium_exports = true` makes export the component build's API (specs/0001-sdk.md, Exports), with `v8_expose_public_symbols = true` for V8's.
- `libcxx_is_shared = true`: libc++ as a shared library the library and the embedder share.
- `is_component_ffmpeg = true`: FFmpeg as its own shared library; `proprietary_codecs = false`, `ffmpeg_branding = "Chromium"`.
- `is_cfi = false`, `use_cfi_icall = false`: control-flow integrity can't allow calls into an embedder.
- `use_remoteexec = false`.

## The patch

`chromium/patches/` holds the SDK's one change to Chromium, as one patch for each repository it touches, applied by `patch`. It adds the GN argument `libchromium_exports`, which:

- defines `LIBCHROMIUM_EXPORTS` for every target, which every export header that keys its `visibility("default")` on `COMPONENT_BUILD` (PartitionAlloc's, on its own `IS_COMPONENT_BUILD` flag) also takes, so a non-component build's shared library exports what a component build's libraries do;
- builds components as source sets in the target's toolchain, so the library takes each whole, as a component build's libraries do, and keeps every export whether anything in it calls the export or not (a non-component build's static libraries would give the link only the objects something refers to);
- keeps `PRESERVE_MOST` off in base and V8, as in a component build;
- gives V8's thread-locals the model a shared library can hold, as in a component build (Blink's heap has an argument of its own for that, `blink_heap_inside_shared_library = true`, among the settings);
- makes Skia, skcms, Abseil, BoringSSL, Perfetto and V8's platform and base libraries, which choose their exports in GN, export as in a component build;
- gives macOS's shared libc++ the `@rpath` install name a component build gives every library, and leaves the allocator shim's `operator new` and `delete` visible there, as a component build does, to match shared libc++'s declarations;
- gives executables and shared libraries the `$ORIGIN` (or `@loader_path`) run path a component build gives them, since libc++ and FFmpeg are shared libraries here and the build runs executables of its own, such as protoc;
- keeps whole-program vtable optimisation off;
- and, whatever the argument, lets a macOS build without V8's context snapshot generate: Chrome's own app bundle, which the SDK never builds but GN always loads, lists V8's snapshot without depending on what makes it.

`scripts/export_patch.py <checkout>/src`, run on a clean checkout, writes the patches: one rule for the export headers, outside trees the SDK never links (Chrome, other platforms, tests), and exact edits, each of which must match once, for the GN files. Moving the pin is: change `chromium/pin.json`, check out the new pin, run `export_patch.py`, and review the patches' differences. `patch` applies each patch unless it is applied already, and starts a repository's patched files again from the pin's when a machine stopped mid-step left it half applied.

## Build

`patch` also copies `chromium/gn/` into the tree as `//libchromium`, only where it changed, so a new checkout of this repository rebuilds nothing. `//libchromium` holds:

- `:chromium`, the shared library, whose public dependencies are the exported targets, so a target that depends on it compiles with their settings and links only the library, a shared library being a link boundary;
- `:embedder_probe`, an embedder in miniature that depends only on `:chromium`; it is never run, and its compile and link commands are the flags the SDK ships;
- `:resources` and `:locales`, the resource packs;
- `:libchromium`, everything a package needs, with ICU's data and V8's snapshot.

`build` runs `autoninja` for `:libchromium`. A failed build fails the step.

## macOS

A Linux machine builds macOS with Chromium's clang and `ld64.lld` against Apple's SDK, which Chromium looks for as a hermetic Xcode in `build/mac_files/xcode_binaries`. `targets.json` pins it: the archive's object, in the private bucket that `CHROMIUM_SDK_APPLE_SDK_BUCKET` names (CI's repository variable `APPLE_SDK_BUCKET`), and its SHA-256. `mac-sdk` downloads it with the machine's Google Cloud sign-in, checks the digest and installs it there, unless that SDK is installed already. Apple's SDK is only ever on build machines: it is never packaged, published or logged. `configure` names its version for Chromium, and sets `use_system_xcode = false`, `enable_dsyms = false` (nothing on Linux can make dSYMs), and `use_v8_context_snapshot = false` (specs/0001-sdk.md, Runtime files).

## Packaging

`package --version <v> --output <dir>` writes the target's parts (specs/0001-sdk.md, Parts) and `assets-<target>.json`, which describes them:

- **Flags.** The probe's compile and link commands, from Siso's build files, become `sdk.json`'s lists. Paths in them become `{sdk}/include/…` (the source tree), `{sdk}/gen/…`, `{sdk}/sysroot`, `{sdk}/lib/…` or `{apple_sdk}`; any other path stops the packaging, so no build machine's path ships.
- **Headers.** The roots are the headers of every target the library links whose directory is part of the public surface (`HEADER_ROOTS` in `scripts/package.py`, and every `//services/*/public`), source and generated, and V8's `include/`. Each root is preprocessed alone with the SDK's flags, or, if that fails, after `build/build_config.h`, which some of Chromium's headers use without including, and every file any of them includes ships, from the source tree into `include/` and from the build into `gen/`. A root that doesn't preprocess alone, such as another platform's, is left out, and its count is in `sdk.json`. The set is complete for every root, and holds nothing they don't include, except Chromium's libc++, libc++abi and libc++'s configuration, which ship whole, since an embedder may use any standard header.
- **Libraries** are copied, their debug information kept in `symbols/` on Linux, and stripped.
- **Toolchain.** Chromium's clang, `lld` and the LLVM tools an embedder's build uses, clang's own headers, and the target's runtime libraries.
- **Notices** (specs/0004-licences.md).

Archives are deterministic: entries sorted, times, owners and modes fixed, compressed with zstd at level 19. A part of 2 GiB or more fails the step. `sources` writes the corresponding source (specs/0004-licences.md) and `assets-sources-<target>.json`.

## By hand

On a Linux x86-64 machine with about 250 GB free, for one target:

```
export LIBCHROMIUM_CHECKOUT=$HOME/libchromium/checkout
for step in checkout packages mac-sdk patch configure build; do python3 scripts/sdk.py $step linux-amd64; done
python3 scripts/sdk.py package linux-amd64 --version 157.0.8103.39-dev --output out
```

For macOS, `mac-sdk` needs access to the bucket, or `--archive <a local copy>` of the pinned archive.
