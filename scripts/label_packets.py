"""라벨용 자료 묶음: 정정마다 API 값 비교, 본문+첨부 낱말 비교, 이후 판 필드별 발췌 (labeling.md 7장).

    uv run python scripts/label_packets.py <출력 폴더> [--focus data/labels/focus-dev.json]

AI 라벨(labeler=claude)을 달 때 본 자료가 이것이다. 엔진의 조각 비교는 쓰지 않는다(글자 그대로
difflib). 첨부 글은 우리 파서 출력이다. 출력에 공고 원문이 들어 있으므로 저장소에 올리지 않는다.
"""

from __future__ import annotations

import difflib
import html
import json
import re
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.db.models import Attachment, NoticeVersion  # noqa: E402
from noticedelta.labeling.store import event_view, load_focus  # noqa: E402
from noticedelta.parsing import ParsedDocument  # noqa: E402

TAG = re.compile(r"<[^>]+>")
KEYS = {
    "deadline": r"마감|접수\s*기간|신청\s*기간|모집\s*기간|접수\s*기한|까지",
    "amount": r"지원\s*금|지원\s*규모|지원\s*한도|최대|만\s*원|억\s*원|천만|백만|지원\s*금액|보조",
    "eligibility_rules": r"신청\s*자격|지원\s*대상|참여\s*대상|자격|제외|업력|요건|대상\s*기업|모집\s*대상",
    "required_documents": r"제출\s*서류|구비\s*서류|증빙|서류|사업계획서|신청서",
    "contact": r"문의|연락처|☎|전화|담당|TEL|Tel|\d{2,4}-\d{3,4}-\d{4}",
}


def clean(s) -> str:
    if s is None:
        return ""
    s = html.unescape(TAG.sub(" ", str(s)))
    return re.sub(r"[ \t\xa0]+", " ", s).strip()


def lines(text: str) -> list[str]:
    return [x.strip() for x in text.splitlines() if x.strip()]


def diff(a: list[str], b: list[str], ctx: int = 1, cap: int = 120) -> list[str]:
    out = []
    for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(ctx):
        out.append("  …")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += [f"  {x[:200]}" for x in a[i1:i2]]
            if tag in ("delete", "replace"):
                out += [f"- {x[:300]}" for x in a[i1:i2]]
            if tag in ("insert", "replace"):
                out += [f"+ {x[:300]}" for x in b[j1:j2]]
    if len(out) > cap:
        out = out[:cap] + [f"  … (비교 {len(out) - cap}줄 더 있음)"]
    return out


def att_text(store, att: Attachment) -> str | None:
    if not att.parsed_key:
        return None
    try:
        return ParsedDocument.from_json(json.loads(store.get(att.parsed_key))).plain_text()
    except Exception as e:  # noqa: BLE001
        return f"(읽기 실패: {e})"


def full_text(session, store, nv: NoticeVersion) -> list[str]:
    out = lines(clean(nv.body))
    atts = session.scalars(sa.select(Attachment).where(Attachment.notice_version_id == nv.id)).all()
    for a in atts:
        t = att_text(store, a)
        if t:
            out += [f"§{a.filename[:12]}§ {x}" for x in lines(t)]
    seen, uniq = set(), []
    for x in out:  # 같은 공고문이 PDF와 HWP로 함께 붙은 경우 겹치는 줄을 한 번만
        k = norm(x)
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def norm(x: str) -> str:
    x = re.sub(r"^§[^§]*§ ", "", x)
    x = html.unescape(TAG.sub("", x)).replace("ㆍ", "·").replace("∙", "·")
    return re.sub(r"\s+", " ", x).strip()


def snippets(all_lines: list[str], pat: str, n: int = 8) -> list[str]:
    rx = re.compile(pat)
    hits = [x[:220] for x in all_lines if rx.search(x)]
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:n]


def word_diff(a_lines: list[str], b_lines: list[str], ctx: int = 8, cap: int = 45) -> list[str]:
    a = " ".join(norm(x) for x in a_lines).split()
    b = " ".join(norm(x) for x in b_lines).split()
    out = []
    for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(ctx):
        parts = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                seg = a[i1:i2]
                parts.append(
                    " ".join(seg)
                    if len(seg) <= 2 * ctx
                    else " ".join(seg[:ctx]) + " … " + " ".join(seg[-ctx:])
                )
            if tag in ("delete", "replace"):
                parts.append("[-" + " ".join(a[i1:i2])[:500] + "-]")
            if tag in ("insert", "replace"):
                parts.append("{+" + " ".join(b[j1:j2])[:500] + "+}")
        out.append("* " + " ".join(parts))
    if len(out) > cap:
        out = out[:cap] + [f"* … (바뀐 곳 {len(out) - cap}군데 더)"]
    return out


def main() -> None:
    out_dir = Path(sys.argv[1])
    focus_path = Path(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--focus" else None
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(f"sqlite:///{ROOT / 'data' / 'dev.db'}")
    store = LocalBlobStore(ROOT / "data" / "snapshots" / "blobs")
    focus = load_focus(focus_path or ROOT / "data" / "labels" / "focus-dev.json")
    with Session(engine) as s:
        for i, (eid, _group) in enumerate(focus, 1):
            v = event_view(s, eid)
            frm = s.get(NoticeVersion, v.ref.from_version.notice_version_id)
            to = s.get(NoticeVersion, v.ref.to_version.notice_version_id)
            p = [
                f"# {i:02d} {v.ref.title}",
                f"event {eid} · v{v.ref.from_version.version_no}→v{v.ref.to_version.version_no}",
                "",
            ]
            p.append("## API 값 (바뀐 키)")
            for k, b, a, changed in v.api_rows:
                if not changed:
                    continue
                cb, ca = clean(b), clean(a)
                if len(cb) > 400 or len(ca) > 400:
                    p.append(f"* {k}: (긴 글, 아래 줄 비교)")
                    p += [
                        "    " + x
                        for x in diff(
                            lines(cb.replace(" - ", "\n- ")),
                            lines(ca.replace(" - ", "\n- ")),
                            cap=40,
                        )
                    ]
                else:
                    p.append(f"* {k}: {cb!r} → {ca!r}")
            p.append("")
            before_all, after_all = full_text(s, store, frm), full_text(s, store, to)
            atts = [f"{r['state']}: {(r['new'] or r['old']).filename}" for r in v.attachments]
            p.append("## 첨부: " + " / ".join(atts))
            p.append("")
            p.append("## 낱말 단위 비교 (본문+첨부 전체, [-이전-] {+이후+})")
            p += word_diff(before_all, after_all) or ["(내용 같음)"]
            p.append("")
            p.append("## 이후 판 필드별 발췌")
            for f, pat in KEYS.items():
                a = snippets(after_all, pat, 4)
                p.append(f"- {f}: " + (" | ".join(norm(x)[:160] for x in a) if a else "(없음)"))
            (out_dir / f"{i:02d}.txt").write_text(
                "\n".join(p) + "\n", encoding="utf-8", errors="replace"
            )
            print(i, eid, len("\n".join(p)))


if __name__ == "__main__":
    main()
