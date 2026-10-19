"""hwp-hwpx-parser 대체 파서. 표준 입력 바이트 → 계약 1 JSON (parse.mjs 참고)."""

import json
import re
import sys
import tempfile

from hwp_hwpx_parser import Reader


def main() -> None:
    mime = sys.argv[1]
    suffix = ".hwpx" if mime == "application/hwp+zip" else ".hwp"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(sys.stdin.buffer.read())
        f.flush()
        try:
            with Reader(f.name) as r:
                text, tables = r.text or "", r.tables or []
        except Exception as e:  # 계약대로 실패를 JSON으로 알린다
            out = {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}", "blocks": []}
        else:
            # 본문 텍스트에는 표가 Markdown으로 섞여 있다. 표 줄은 빼고 표는 따로 넣는다.
            paras = [
                p.strip()
                for p in re.split(r"\n\s*\n", text)
                if p.strip() and not p.strip().startswith("|")
            ]
            blocks = [{"type": "paragraph", "text": p, "level": None, "page": None} for p in paras]
            blocks += [{"type": "table", "rows": t.rows, "page": None} for t in tables if t.rows]
            out = {"status": "ok", "error": None, "blocks": blocks}
    out.update(contract=1, parser="hwp-hwpx-parser", parser_version="1.0.0", warnings=0)
    sys.stdout.write("\n@@PARSED@@" + json.dumps(out, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
