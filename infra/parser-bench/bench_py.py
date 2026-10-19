"""Python 파서 실행기. /in/manifest.json의 파일을 하나씩 별도 프로세스로 파싱한다.

출력: /out/<파서>/<key>.txt (추출 텍스트), stdout에 파일·파서별 결과 JSON 한 줄.
참조(reference)는 파서가 아니라 채점용 기준 텍스트다.
- HWPX: Contents/section*.xml의 <hp:t> 텍스트와 <hp:tbl> 개수
- HWP5: OLE의 PrvText 스트림(문서 앞부분 미리보기 텍스트)
"""

import json
import multiprocessing as mp
import re
import sys
import time
import zipfile
from pathlib import Path

IN, OUT = Path("/in"), Path("/out")
TIMEOUT_S = 60


def hhp(path, mime):
    import shutil

    from hwp_hwpx_parser import Reader

    # 이 파서는 확장자로 형식을 고른다. 저장소의 파일 이름은 해시라 확장자를 붙여 복사한다.
    suffix = ".hwpx" if mime == "application/hwp+zip" else ".hwp"
    tmp = Path("/tmp") / f"input{suffix}"
    shutil.copyfile(path, tmp)
    with Reader(str(tmp)) as r:
        text = r.text or ""
        tables = r.tables or []
    cells = 0
    for t in tables:
        rows = getattr(t, "data", None) or getattr(t, "rows", None) or []
        cells += sum(len(row) for row in rows)
    return text, len(tables), cells


def pdfplumber_(path, mime):
    import pdfplumber

    texts, n_tables, cells = [], 0, 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            for t in page.extract_tables():
                n_tables += 1
                cells += sum(len(row) for row in t)
    return "\n".join(texts), n_tables, cells


def reference(path, mime):
    if mime == "application/hwp+zip":
        z = zipfile.ZipFile(path)
        parts, n_tables = [], 0
        for name in sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n)):
            xml = z.read(name).decode("utf-8", "replace")
            n_tables += len(re.findall(r"<hp:tbl[\s>]", xml))
            parts += re.findall(r"<hp:t(?:\s[^>]*)?>([^<]*)</hp:t>", xml)
        return "\n".join(parts), n_tables, 0
    if mime == "application/x-hwp":
        import olefile

        with olefile.OleFileIO(str(path)) as ole:
            if not ole.exists("PrvText"):
                return "", 0, 0
            return ole.openstream("PrvText").read().decode("utf-16-le", "replace"), 0, 0
    return "", 0, 0


PARSERS = {
    "hwp-hwpx-parser": (hhp, {"application/x-hwp", "application/hwp+zip"}),
    "pdfplumber": (pdfplumber_, {"application/pdf"}),
    "reference": (reference, {"application/x-hwp", "application/hwp+zip"}),
}


def _child(name, path, mime, q):
    try:
        text, n_tables, cells = PARSERS[name][0](path, mime)
        q.put({"ok": True, "text": text, "tables": n_tables, "cells": cells})
    except Exception as e:  # 파서마다 예외 종류가 달라 모두 실패로 기록한다
        q.put({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})


def run_one(name, key, path, mime):
    q = mp.Queue()
    p = mp.Process(target=_child, args=(name, path, mime, q))
    t0 = time.perf_counter()
    p.start()
    p.join(TIMEOUT_S)
    ms = round((time.perf_counter() - t0) * 1000)
    if p.is_alive():
        p.kill()
        return {"ok": False, "error": "timeout", "ms": ms}
    res = q.get() if not q.empty() else {"ok": False, "error": f"exit {p.exitcode}"}
    res["ms"] = ms
    if res.get("ok"):
        out = OUT / name
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{key}.txt").write_text(res.pop("text"), encoding="utf-8")
    return res


def main():
    manifest = json.loads((IN / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        for name, (_, mimes) in PARSERS.items():
            if item["mime"] not in mimes:
                continue
            res = run_one(name, item["key"], IN / "blobs" / item["key"], item["mime"])
            print(json.dumps({"key": item["key"], "parser": name, **res}), flush=True)


if __name__ == "__main__":
    mp.set_start_method("fork")
    sys.exit(main())
