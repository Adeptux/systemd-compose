# Nuitka Linux Packaging

This project ships a Docker-based Nuitka builder for release binaries:

```bash
scripts/build-nuitka-linux.sh
```

The default builder image is `quay.io/pypa/manylinux2014_x86_64`, which is based
on a glibc 2.17 baseline. Building on an older glibc is the useful part of the
Ubuntu 20.04 idea: the resulting binary can usually run on distributions with
that glibc version or newer. Ubuntu 20.04 has glibc 2.31, so manylinux2014 gives
an even wider target for x86_64 Linux.

The Dockerfile unpacks manylinux's bundled static Python libraries before the
Nuitka step. Nuitka needs those on manylinux because the image does not provide
shared `libpython` builds.

Artifacts are copied to `dist/nuitka/`:

- `systemd-compose-linux-x86_64` - the onefile executable
- `SHA256SUMS` - checksum for the executable
- `help.txt` - smoke-test output from `systemd-compose --help`
- `ldd-version.txt` - builder glibc version
- `glibc-symbols.txt` - versioned glibc symbols referenced by the executable

Runtime requirements still come from what the CLI controls on the host:
`systemctl --user`, `systemd-run`, `journalctl`, and `/usr/bin/bwrap` must exist
on the target machine.

The builder uses `--onefile-no-compression`. This makes the executable larger,
but avoids zstd memory spikes during onefile packing and keeps the release build
more reliable in constrained Docker or CI environments.

Useful knobs:

```bash
PLATFORM=linux/amd64 scripts/build-nuitka-linux.sh
IMAGE=systemd-compose-nuitka:local scripts/build-nuitka-linux.sh
NUITKA_VERSION=4.1.3 scripts/build-nuitka-linux.sh
```

For aarch64, use a matching manylinux image such as
`quay.io/pypa/manylinux2014_aarch64` and build on native aarch64 hardware or
with a configured Docker/QEMU setup.
