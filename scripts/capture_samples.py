"""실제 API 응답을 받아 로컬에 저장하고 응답 형태를 요약한다 (4주차 첫 호출 확인용).

    uv run python scripts/capture_samples.py [--rows 5]

- 인증키는 .env(또는 환경 변수)의 KSTARTUP_SERVICE_KEY, BIZINFO_SERVICE_KEY, BIZINFO_CRTFC_KEY.
  키가 없는 출처는 건너뛴다.
- 원본 응답은 data/samples/에 저장한다(.gitignore). 약관상 원문을 저장소에 올리지 않고,
  테스트 픽스처는 여기서 확인한 형태를 따라 가상 값으로 만든다 (docs/research/terms.md).
- 요약에는 응답 껍데기, 항목 필드와 타입, 비어 있는 비율만 찍고 값은 찍지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import httpx

from noticedelta.config import load_env
from noticedelta.sources import bizinfo, kstartup
from noticedelta.sources.base import normalize_service_key

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "samples"


def targets(rows: int) -> list[tuple[str, str, str, dict]]:
    """(이름, 키 변수, URL, 키를 뺀 쿼리)."""
    return [
        (
            "kstartup",
            "KSTARTUP_SERVICE_KEY",
            kstartup.ENDPOINT,
            {"keyName": "serviceKey", "page": 1, "perPage": rows, "returnType": "json"},
        ),
        (
            "bizinfo_data_go_kr",
            "BIZINFO_SERVICE_KEY",
            bizinfo.DATA_GO_KR_ENDPOINT,
            {"keyName": "serviceKey", "pageNo": 1, "numOfRows": rows, "dataType": "json"},
        ),
        (
            "bizinfo_own",
            "BIZINFO_CRTFC_KEY",
            bizinfo.BIZINFO_ENDPOINT,
            {"keyName": "crtfcKey", "pageIndex": 1, "pageUnit": rows, "dataType": "json"},
        ),
    ]


def shape(v, depth: int = 0):
    """값 없이 구조만. 배열은 첫 항목 형태와 길이."""
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [f"len={len(v)}", shape(v[0], depth + 1)] if v else ["len=0"]
    return type(v).__name__


def find_items(payload) -> list[dict]:
    """가장 큰 dict 배열을 항목 목록으로 본다."""
    best: list[dict] = []

    def walk(v):
        nonlocal best
        if isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v) and len(v) > len(best):
                best = v
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(payload)
    return best


def field_report(items: list[dict]) -> dict[str, str]:
    keys = sorted({k for it in items for k in it})
    out = {}
    for k in keys:
        vals = [it.get(k) for it in items]
        types = sorted({type(x).__name__ for x in vals})
        empty = sum(1 for x in vals if x in (None, "", [], {}))
        out[k] = f"{'/'.join(types)} empty={empty}/{len(vals)}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=5)
    args = ap.parse_args()
    load_env()
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    with httpx.Client(timeout=30) as http:
        for name, env, url, params in targets(args.rows):
            key = os.environ.get(env)
            if not key:
                print(f"[{name}] {env} 없음, 건너뜀")
                continue
            params = dict(params)
            params[params.pop("keyName")] = normalize_service_key(key)
            try:
                resp = http.get(url, params=params)
            except httpx.HTTPError as e:
                print(f"[{name}] 호출 실패: {type(e).__name__}")
                continue
            ctype = resp.headers.get("content-type", "")
            print(f"[{name}] HTTP {resp.status_code} content-type={ctype}")
            raw = OUT / f"{name}-{stamp}.txt"
            raw.write_text(resp.text, encoding="utf-8")
            try:
                payload = resp.json()
            except ValueError:
                # data.go.kr는 키 오류를 XML로 돌려주는 경우가 있다. 앞부분만 보여 준다.
                print(f"  JSON 아님. 앞부분: {resp.text[:300]!r}")
                continue
            raw.rename(raw.with_suffix(".json"))
            items = find_items(payload)
            summary = {
                "envelope": shape(payload),
                "item_count": len(items),
                "fields": field_report(items),
            }
            (OUT / f"{name}-{stamp}.shape.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
