"""HWP·HWPX·PDF 파서 품질 비교 (작업 목록 3번, docs/research/parsers.md).

    uv run python scripts/compare_parsers.py prepare     # data/corpus.db → data/parser-bench/in
    bash infra/parser-bench/run.sh data/parser-bench/in data/parser-bench/out
    uv run python scripts/compare_parsers.py report      # 결과 표

채점 기준 (파일·파서마다):
- ok: 예외·시간 초과 없이 끝남
- 내용 있음: 공백을 뺀 글자가 50자 이상, 글자 중 한글 비율 30% 이상
- 기준 텍스트 재현율: 기준 텍스트의 문자 3-gram 중 출력에 있는 비율
  (HWPX는 section XML의 <hp:t> 전체, HWP5는 PrvText 미리보기)
- 성공 = ok + 내용 있음 + (기준 텍스트가 있으면 재현율 0.8 이상)
- 표: HWPX는 <hp:tbl> 개수와 같은지
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DB = ROOT / "data" / "corpus.db"
BLOBS = ROOT / "data" / "corpus-blobs"
BENCH = ROOT / "data" / "parser-bench"
TARGET_MIMES = {"application/x-hwp", "application/hwp+zip", "application/pdf"}
LABEL = {"application/x-hwp": "HWP", "application/hwp+zip": "HWPX", "application/pdf": "PDF"}

_KEEP = re.compile(r"[가-힣A-Za-z0-9]")


_MARKUP = re.compile(r"<br\s*/?>|<[/a-z][^>]*>|^\s*\|?\s*-{3,}.*$", re.IGNORECASE | re.MULTILINE)


def strip_markup(text: str) -> str:
    """Markdown·HTML 표기(<br>, 표 구분선 등)를 지운다. 칸 안 줄바꿈을 <br>로 쓰는 파서가
    'br' 글자 때문에 재현율·한글 비율에서 손해 보지 않게 한다."""
    return _MARKUP.sub(" ", text)


def _norm(text: str) -> str:
    return "".join(_KEEP.findall(strip_markup(text)))


def _grams(s: str, n: int = 3) -> set[str]:
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def recall(reference: str, output: str) -> float | None:
    ref = _grams(_norm(reference))
    if len(ref) < 20:
        return None
    return len(ref & _grams(_norm(output))) / len(ref)


def hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum("가" <= c <= "힣" for c in letters) / len(letters)


def prepare() -> None:
    con = sqlite3.connect(CORPUS_DB)
    rows = con.execute(
        "select distinct blob_key, mime, filename, role from attachment where blob_key is not null"
    ).fetchall()
    inp = BENCH / "in"
    (inp / "blobs").mkdir(parents=True, exist_ok=True)
    manifest, seen = [], set()
    for key, mime, filename, role in rows:
        if mime not in TARGET_MIMES or key in seen:
            continue
        seen.add(key)
        shutil.copyfile(BLOBS / key[:2] / key[2:4] / key, inp / "blobs" / key)
        manifest.append({"key": key, "mime": mime, "filename": filename, "role": role})
    (inp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    by = defaultdict(int)
    for m in manifest:
        by[LABEL[m["mime"]]] += 1
    print(f"파일 {len(manifest)}개: {dict(by)}")


def _p95(xs: list[int]) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))] if xs else 0


def report() -> None:
    out = BENCH / "out"
    manifest = {
        m["key"]: m
        for m in json.loads((BENCH / "in" / "manifest.json").read_text(encoding="utf-8"))
    }
    results = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    ref = {r["key"]: r for r in results if r["parser"] == "reference" and r.get("ok")}

    def text_of(parser: str, key: str) -> str:
        p = out / parser / f"{key}.txt"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    rows = []
    for r in results:
        if r["parser"] == "reference":
            continue
        m = manifest[r["key"]]
        text = text_of(r["parser"], r["key"]) if r.get("ok") else ""
        plain = strip_markup(text)
        chars = len(re.sub(r"\s", "", plain))
        hr = hangul_ratio(plain)
        rref = ref.get(r["key"])
        rec = recall(text_of("reference", r["key"]), text) if rref and r.get("ok") else None
        content = r.get("ok") and chars >= 50 and hr >= 0.3
        success = bool(content and (rec is None or rec >= 0.8))
        tables_match = None
        if m["mime"] == "application/hwp+zip" and rref and r.get("ok"):
            tables_match = r.get("tables") == rref.get("tables")
        rows.append(
            {
                **r,
                "mime": m["mime"],
                "role": m["role"],
                "chars": chars,
                "hangul": round(hr, 3),
                "recall": None if rec is None else round(rec, 3),
                "success": success,
                "tables_match": tables_match,
            }
        )
    (BENCH / "report.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["parser"], LABEL[r["mime"]])].append(r)
    print(
        "| 파서 | 형식 | 파일 | 오류 없이 끝남 | 성공 | 재현율 중앙값 | 표 개수 일치 | 시간 중앙값 / p95 (ms) |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for (parser, fmt), rs in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        n = len(rs)
        ok = sum(bool(r.get("ok")) for r in rs)
        succ = sum(r["success"] for r in rs)
        recs = [r["recall"] for r in rs if r["recall"] is not None]
        tm = [r["tables_match"] for r in rs if r["tables_match"] is not None]
        ms = [r["ms"] for r in rs if r.get("ok")]
        rec_s = f"{statistics.median(recs):.3f}" if recs else "-"
        tm_s = f"{sum(tm)}/{len(tm)}" if tm else "-"
        ms_s = f"{statistics.median(ms) if ms else 0:.0f} / {_p95(ms)}"
        print(
            f"| {parser} | {fmt} | {n} | {ok} ({ok / n:.0%}) | {succ} ({succ / n:.0%}) | "
            f"{rec_s} | {tm_s} | {ms_s} |"
        )

    # 파서 하나를 고를 때의 성공률(형식 전체), 그리고 형식별 가장 나은 파서를 고를 때
    by_file: dict[str, dict[str, bool]] = defaultdict(dict)
    for r in rows:
        by_file[r["key"]][r["parser"]] = r["success"]
    total = len(by_file)
    any_ok = sum(any(v.values()) for v in by_file.values())
    print(f"\n파일 {total}개 중 하나 이상의 파서가 성공: {any_ok} ({any_ok / total:.0%})")
    for parser in sorted({r["parser"] for r in rows}):
        cover = [k for k, v in by_file.items() if parser in v]
        ok = sum(by_file[k][parser] for k in cover)
        print(f"{parser}: 처리 대상 {len(cover)}개 중 성공 {ok} ({ok / len(cover):.0%})")
    # 모든 파서가 PDF에서 글자를 거의 못 뽑았으면 파서 문제가 아니라 텍스트 층이 없는 스캔본이다.
    pdf_keys = {r["key"] for r in rows if r["mime"] == "application/pdf"}
    scanned = sorted(
        k for k in pdf_keys if all(r["chars"] < 50 for r in rows if r["key"] == k and r.get("ok"))
    )
    print(f"텍스트 층이 없는 PDF(스캔본 추정): {len(scanned)}/{len(pdf_keys)}")
    for parser in sorted({r["parser"] for r in rows if r["mime"] == "application/pdf"}):
        rs = [r for r in rows if r["parser"] == parser and r["key"] in pdf_keys - set(scanned)]
        print(f"  스캔본 제외 PDF / {parser}: {sum(r['success'] for r in rs)}/{len(rs)}")
    notice = [r for r in rows if r["role"] == "notice"]
    for parser in sorted({r["parser"] for r in notice}):
        rs = [r for r in notice if r["parser"] == parser]
        print(f"공고문 본 파일 / {parser}: {sum(r['success'] for r in rs)}/{len(rs)}")
    errors: dict[str, int] = defaultdict(int)
    for r in rows:
        if not r.get("ok"):
            errors[f"{r['parser']}: {str(r.get('error'))[:80]}"] += 1
    if errors:
        print("\n실패 사유:")
        for e, n in sorted(errors.items(), key=lambda kv: -kv[1]):
            print(f"  {n}건  {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "report"])
    {"prepare": prepare, "report": report}[ap.parse_args().cmd]()
