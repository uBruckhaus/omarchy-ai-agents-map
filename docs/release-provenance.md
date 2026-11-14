# Provenance of the pinned releases

`release_pins.py` names every program file AI Agents Map installs or updates itself, with its
SHA-256 (SHA-512 for the Antigravity CLI). This file records where each pinned file comes from:
the upstream source commit its tag points to, how the project builds and publishes the file,
any build attestation GitHub holds for it, and the licenses involved. It was compiled on
2026-11-14 from the GitHub API (`git/ref/tags`, `releases/tags`, `attestations/<digest>`) and the
projects' repositories; the archives' contents were not unpacked and inspected here, so bundled
parts are named as the projects describe them.

AI Agents Map does not build, change or redistribute these files. It downloads each one from the
project's own release on the user's click, checks it against the pinned checksum and installs it
for that user only. A newer version reaches users only with a plugin update that changes
`release_pins.py` and this file together.

| Program | Pinned tag | Source commit of the tag | Built and published by | Build attestation | License |
| --- | --- | --- | --- | --- | --- |
| Ollama | `v0.40.16` | [`d63b702`](https://github.com/ollama/ollama/commit/d63b702609c4300a2ca07be2bdb0adb9834725e5) | release by `github-actions[bot]`, workflow `.github/workflows/release.yaml` | none published | MIT |
| llama.cpp | `b12383` | [`c56be86`](https://github.com/ggml-org/llama.cpp/commit/c56be864f482dc88a6d62a0816a265034941935f) | release by `github-actions[bot]`; the build workflows `build-*.yml`, published by `release-publish.yml` | SLSA v1 provenance for every pinned file: builder `.github/workflows/release-publish.yml@refs/heads/master`, source commit `3549c27` (the publishing run; the tag points to `c56be86`) | MIT |
| KoboldCpp | `v1.124.2` | [`6d2fedf`](https://github.com/LostRuins/koboldcpp/commit/6d2fedf8624e5f05926af3191f9efdcb13c4133c) | release published by the maintainer (`LostRuins`); Linux files built by `kcpp-build-release-linux*.yaml` | none published | AGPL-3.0 |
| Jan | `v0.8.8` | [`a7752d5`](https://github.com/janhq/jan/commit/a7752d5be16fc5d92df7809def274ffd2b27db07) | release by `github-actions[bot]`, workflow `jan-tauri-build.yaml` (Linux: `template-tauri-build-linux-x64.yml`) | none published | Apache-2.0 |
| mise | `v2026.11.8` | [`a4a1996`](https://github.com/jdx/mise/commit/a4a1996f49779db556570678c81f658e5a28fe19) | release by `mise-en-dev`, workflow `.github/workflows/release.yml` | SLSA v1 provenance: builder `.github/workflows/release.yml@refs/tags/v2026.11.8`, source commit `a4a1996` (the tag's) | MIT |
| Antigravity CLI | `1.5.9` | not public | Google's file on `storage.googleapis.com/antigravity-public/antigravity-cli/` (the one Google's own installer takes) | none | Google's terms (antigravity.google) |

## Bundled parts and their licenses

- **Ollama** `ollama-linux-amd64.tar.zst` carries Ollama's GPU runners together with NVIDIA's CUDA
  runtime libraries (NVIDIA CUDA Toolkit EULA, redistributable parts); `ollama-linux-amd64-rocm.tar.zst`
  (AMD cards) carries AMD ROCm libraries (their own licenses, mostly MIT).
- **llama.cpp** `cudart-llama-b12383-bin-ubuntu-cuda-12.8-x64.tar.gz` is NVIDIA's CUDA runtime
  (NVIDIA CUDA Toolkit EULA, redistributable parts), fetched only for the CUDA build; the ROCm build
  links AMD ROCm libraries; the Vulkan and CPU builds are llama.cpp alone (MIT, with the third-party
  notices in its repository).
- **KoboldCpp** `koboldcpp-linux-x64` (CUDA) is a single file that carries NVIDIA CUDA libraries
  (NVIDIA EULA); `koboldcpp-linux-x64-nocuda` carries none. KoboldCpp includes llama.cpp (MIT).
- **Jan** `Jan_0.8.8_amd64.AppImage` carries the Jan app (Tauri), its llama.cpp-based engine (MIT)
  and the libraries an AppImage brings along.
- **mise** is one static (musl) program.
- **Antigravity CLI** is Google's program under Google's terms.

## Not covered by the pins

Agents installed with **mise** (the Install… and Update… recipes on agent cards, `mise use` and
`mise upgrade`) and **Install Node.js** in the first-agent guide (`mise use -g node@lts`) run mise in
a terminal you watch, like Omarchy's own installer: mise chooses the version at that moment and
checks downloads only where its backends publish checksums; nothing about them is pinned here.
Claude Code's own updater (`claude update`) works the same way.
