#!/bin/sh
set -eu
cd "$(dirname "$0")"
archive=${1:-images/build-inputs-amd64.tar}
python3 - "$archive" <<'PY'
import hashlib, pathlib, sys
assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == "28411dc92ee85ae55d7d38d816ace14f2b4b303216011602127317b14b716b35", "Build-input archive checksum mismatch"
PY
docker load --input "$archive"
docker build --platform linux/amd64 --pull=false \
  --build-arg REPOCKET_IMAGE=wdi-repocket-base:1.2.0 \
  --build-arg TOOLBOX_IMAGE=wdi-repocket-toolbox:1.37.0-musl \
  --tag wdi-repocket:rebuild .
