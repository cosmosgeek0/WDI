#!/bin/sh
set -eu
cd "$(dirname "$0")"
archive=${1:?Usage: ./build.sh /path/to/production-image.tar}
base=sha256:42be97a52c1d2eacb677898eba02d1a513d2eb559fe92a9122b6b7d5a986dd4a
docker load --input "$archive"
test "$(docker image inspect --format '{{.Id}}' "$base")" = "$base"
docker tag "$base" wdi-grass-base:7.6.1-v3
repocket=sha256:45cd81b05271de652259fdbed044505b41fe770755192c4b99eb370c30df0647
docker load --input "${2:-../Repocket/images/repocket-1.2.0-filtered-45cd81b05271.tar}"
docker tag "$repocket" wdi-repocket:combined-base
python3 -c 'from pathlib import Path; import hashlib; Path("worker.sha256").write_text(hashlib.sha256(Path("worker.pl").read_bytes()).hexdigest()+"\n")'
build_context=$(mktemp -d)
trap 'rm -rf "$build_context"' EXIT HUP INT TERM
cp Dockerfile .dockerignore supervisor.py session.py worker.pl worker.sha256 ip_policy.py node_auth.py "$build_context/"
cp ../Repocket/traffic/traffic.py "$build_context/traffic.py"
cp ../Repocket/traffic/bin/strace ../Repocket/traffic/LICENSE.strace "$build_context/"
test "$(shasum -a 256 "$build_context/strace" | cut -d ' ' -f 1)" = f483a6f00c27c723e3ea1907f0d236b16f4e236049da03b0153a7db2789b13d7
chmod 755 "$build_context/strace"
docker build --platform linux/amd64 --pull=false --build-arg REPOCKET_BASE=wdi-repocket:combined-base --tag wdi-grass:node-auth "$build_context"
docker run --rm --platform linux/amd64 --network none --entrypoint /bin/sh wdi-grass:node-auth -c 'test -L /bin && test -x /bin/busybox && /usr/local/bin/strace -V'
