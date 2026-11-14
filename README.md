# Chromium SDK

Prebuilt Chromium for embedders of its Content API. Each version is upstream Chromium at one release tag, built with fixed settings as one shared library that exports the Content API, and shipped with what an embedder needs to compile and link its own code against it: the headers, generated ones included, the build settings, and Chromium's own toolchain, its clang and its libc++. An embedder pins a version, compiles only its own code and links the library; it never compiles Chromium. Moving to a new Chromium release, or to new settings, is moving to a new version.

Targets are `linux-amd64`, `linux-arm64` and `macos-arm64`. Apple's SDK is never part of a version: a macOS embedder brings its own.

## Licences

This repository's own files are under the MIT License (`LICENSE`). The binaries it publishes are Chromium's, under Chromium's licences: each version ships Chromium's `LICENSE`, the credits for every third-party library the build links, and the corresponding source of every component whose licence asks for it, such as FFmpeg's under the LGPL, published from the same place as the binaries.
