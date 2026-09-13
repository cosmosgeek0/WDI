#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -f strace-6.8.tar.xz ]; then
    curl -fLsS https://strace.io/files/6.8/strace-6.8.tar.xz -o strace-6.8.tar.xz
fi
python3 - <<'PY'
import hashlib, pathlib
assert hashlib.sha256(pathlib.Path('strace-6.8.tar.xz').read_bytes()).hexdigest() == 'ba6950a96824cdf93a584fa04f0a733896d2a6bc5f0ad9ffe505d9b41e970149'
PY
mkdir -p bin
docker run --rm -v "$PWD:/work" -w /work \
  debian@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 \
  /bin/sh -ec '
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc libc6-dev make xz-utils >/dev/null
if [ "$(dpkg --print-architecture)" != amd64 ]; then
    apt-get install -y -qq --no-install-recommends gcc-x86-64-linux-gnu libc6-dev-amd64-cross >/dev/null
    # The cross libc math linker script uses these absolute native paths.
    mkdir -p /usr/lib/x86_64-linux-gnu
    ln -sf /usr/x86_64-linux-gnu/lib/libm-2.36.a /usr/lib/x86_64-linux-gnu/libm-2.36.a
    ln -sf /usr/x86_64-linux-gnu/lib/libmvec.a /usr/lib/x86_64-linux-gnu/libmvec.a
fi
directory=$(mktemp -d)
tar -xf /work/strace-6.8.tar.xz -C "$directory"
cd "$directory/strace-6.8"
./configure --host=x86_64-linux-gnu --enable-mpers=no --enable-stacktrace=no CFLAGS=-O2 LDFLAGS=-static > configure.log
make -j2 > build.log
x86_64-linux-gnu-strip src/strace
install -m 755 src/strace /work/bin/strace
cp COPYING /work/LICENSE.strace
sha256sum /work/bin/strace
'
