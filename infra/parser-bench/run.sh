#!/usr/bin/env bash
# 파서 비교 실행. 입력 폴더(manifest.json + blobs/)는 읽기 전용, 네트워크 없음, 자원 제한, 비루트.
# 사용: infra/parser-bench/run.sh <입력 폴더> <출력 폴더>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
in="$(cd "$1" && pwd)"; mkdir -p "$2"; out="$(cd "$2" && pwd)"
docker build -q -t noticedelta-bench-py -f "$here/py.Dockerfile" "$here" >/dev/null
docker build -q -t noticedelta-bench-node -f "$here/node.Dockerfile" "$here" >/dev/null
chmod -R a+rwX "$out"
for img in noticedelta-bench-py noticedelta-bench-node; do
  MSYS_NO_PATHCONV=1 docker run --rm --network none --read-only --tmpfs /tmp \
    --memory 1g --cpus 1 --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges \
    -v "$in:/in:ro" -v "$out:/out" "$img"
done > "$out/results.jsonl"
wc -l "$out/results.jsonl"
