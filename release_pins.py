"""The exact releases AI Agents Map installs, pinned in its own source.

Nothing is chosen at run time: every program the app installs or updates itself is one
release named here, with its files, their download URLs, sizes and the checksum each must
match before anything is unpacked (install_ctl.Installer.install). A newer version of one
of these programs reaches users only with a plugin update that changes this file, so
every executable it can install is a reviewed artifact. GitHub URLs are built from the
repository, tag and file name below; nothing from a release listing is used.

License of each program (they are installed and run as separate programs):
Ollama MIT · llama.cpp MIT · KoboldCpp AGPL-3.0 · Jan Apache-2.0 · mise MIT ·
Antigravity CLI Google's terms (antigravity.google).
"""

# installer key → (repository, tag, {build: [(file name, size, "sha256:<hex>")]})
GITHUB = {
    "ollama": ("ollama/ollama", "v0.40.16", {
        "": [("ollama-linux-amd64.tar.zst", 1545767640,
              "sha256:87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d")],
        "rocm": [("ollama-linux-amd64.tar.zst", 1545767640,
                  "sha256:87bfafb2acbc89b2e6a1be0a9043de6aa75fb450696f86bc426e65fc448f788d"),
                 ("ollama-linux-amd64-rocm.tar.zst", 1053876146,
                  "sha256:50ba6b7d9f4ae9b143c5c20496bf82be597dab0a06a50276b637e22150226f9b")]}),
    "llamacpp": ("ggml-org/llama.cpp", "b12383", {
        "vulkan": [("llama-b12383-bin-ubuntu-vulkan-x64.tar.gz", 36191252,
                    "sha256:6c596df86ee0af1e71798eb211304a2b5e448d344032faa422eb6d7bd638b97f")],
        "rocm": [("llama-b12383-bin-ubuntu-rocm-10.0-x64.tar.gz", 271144114,
                  "sha256:d1fcd3e9569919d3f82c4eae38a9268226ad410657bd8bfdfa882be1295b8b01")],
        "cuda": [("llama-b12383-bin-ubuntu-cuda-12.8-x64.tar.gz", 199480829,
                  "sha256:adb5614fee53ae29f645b1174c46fd2645f0ba2eb12f142d81e427150b47594a"),
                 ("cudart-llama-b12383-bin-ubuntu-cuda-12.8-x64.tar.gz", 594371474,
                  "sha256:02bae394724ecb22ee65883584fe6e8015b3cb0f132ec26aca142782d8c42f5a")],
        "cpu": [("llama-b12383-bin-ubuntu-x64.tar.gz", 23336228,
                 "sha256:8e1d9d147d7f0dd2399fb32381de126ebc926d452225ef3b19e1675a55e0c794")]}),
    "koboldcpp": ("LostRuins/koboldcpp", "v1.124.2", {
        "cuda": [("koboldcpp-linux-x64", 628981944,
                  "sha256:c66ae0106673c7d9c41bafcc92c95bc5a6256709e9d853e5e2255bf536c1b069")],
        "vulkan": [("koboldcpp-linux-x64-nocuda", 107362352,
                    "sha256:e7bc9457030fbbc454248d801d9f6971fc4b2d3fd16e0aec71f064eed26a05f4")],
        "cpu": [("koboldcpp-linux-x64-nocuda", 107362352,
                 "sha256:e7bc9457030fbbc454248d801d9f6971fc4b2d3fd16e0aec71f064eed26a05f4")]}),
    "jan": ("janhq/jan", "v0.8.8", {
        "": [("Jan_0.8.8_amd64.AppImage", 712755704,
              "sha256:ef98314c0230e3066235fd195ff1ffdf652b85b264bbd9ddf337f99d077c9159")]}),
    "mise": ("jdx/mise", "v2026.11.8", {
        "x64": [("mise-v2026.11.8-linux-x64-musl.tar.gz", 56334728,
                 "sha256:459ac6f5c1581b44987347af09ddcb518ef255631a098e3ec0430982555eb138")],
        "arm64": [("mise-v2026.11.8-linux-arm64-musl.tar.gz", 62734777,
                   "sha256:8b3b9d3508a4fc1bee5f90b0bea1926f5656f40f58e40374616daeaaf8d3587c")]}),
}

# Antigravity CLI: Google's own release files (storage.googleapis.com) with their SHA-512.
AGY = ("1.5.9", {
    "amd64": ("https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.9-4515810895593472/linux-x64/cli_linux_x64.tar.gz",
              "sha512:d01eb13ba7750eed18907f1ae8860864371e0ad9064415e8d033d22b4bd7b9b4e4c7f962058520b452d67db1c33163d9a889d55a7dffa159596ec6cd91a87b79"),
    "arm64": ("https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.9-4515810895593472/linux-arm/cli_linux_arm64.tar.gz",
              "sha512:16c632a09b81f598f7652bc11180861f9d2ee4408ffa161dadc83b2010de7a9cd055e97b4b46cd01903e35c0e8bf21d8262462ba7c5f168cc6dada9ac3d522c0"),
})


def github_files(key: str, build: str):
    """(tag, [(name, url, size, hex digest)]) of the pinned release, or a RuntimeError."""
    repo, tag, builds = GITHUB[key]
    files = builds.get(build)
    if files is None:
        raise RuntimeError(f"no pinned {build or 'default'} build")
    return tag, [(name, f"https://github.com/{repo}/releases/download/{tag}/{name}", size, digest.split(":", 1)[1])
                 for name, size, digest in files]
