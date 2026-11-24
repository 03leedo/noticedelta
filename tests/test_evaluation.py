"""평가 골격 (작업 목록 14·15번). 가상 정정과 가짜 추출기로 방식·지표·표를 확인한다."""

import json
import re
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from conftest import sqlite_engine
from sqlalchemy.orm import Session

from noticedelta.db import models
from noticedelta.evaluation import (
    B0Meta,
    B2Full,
    B3Diff,
    Chain,
    Engine,
    EvalEvent,
    Fields,
    Label,
    disagreements,
    evaluate,
    input_sizes,
    load_chains,
    load_labels,
    render,
    score,
    split_of,
    wilson,
)
from noticedelta.impact import FieldValue
from noticedelta.llm import FileCassetteStore, Gateway, LLMResponse, MissingCassette, Mode
from noticedelta.matching import ChangeSet
from noticedelta.programs import api_meta
from noticedelta.segments import Segment


def seg(lid, text):
    return Segment("paragraph", text, f"h-{lid}-{len(text)}", {}, int(lid[1:]), lineage_id=lid)


def fake_extractor(fields, segments):
    """가짜 LLM: "마감 YYYY-MM-DD"와 "지원금 …원"을 찾아 값과 근거 조각을 낸다."""
    out = {}
    for s in segments:
        if "deadline" in fields and (m := re.search(r"마감\D*(\d{4}-\d{2}-\d{2})", s.text)):
            out["deadline"] = FieldValue("deadline", "datetime", m.group(1), [s.lineage_id])
        if "amount" in fields and (m := re.search(r"지원금\D*([\d,]+)\s*(만)?\s*원", s.text)):
            won = int(m.group(1).replace(",", "")) * (10_000 if m.group(2) else 1)
            out["amount"] = FieldValue("amount", "amount", won, [s.lineage_id])
    return out


SPEC = Fields(
    {"deadline": "datetime", "amount": "amount"},
    guide={"deadline": "신청 마감", "amount": "지원 금액"},
    meta=("deadline",),
    diff_system="바뀐 필드를 판단한다",
)
V1 = [
    seg("s1", "신청 마감 2026-11-07"),
    seg("s2", "지원금 5,000만원"),
    seg("s3", "문의는 사업운영팀"),
]
V2 = [seg("s1", "신청 마감 2026-11-14"), V1[1], V1[2]]  # 마감 연장: API 메타에도 보인다
V3 = [V2[0], seg("s2", "지원금 50,000,000원 (오기 정정)"), V2[2]]  # 문구만 바뀜
V4 = [V3[0], seg("s2", "지원금 3,000만원"), V3[2]]  # 금액 감소: 첨부에서만 보인다


def event(eid, old, new, modified, before, after):
    unchanged = [s.lineage_id for s in new if s.lineage_id not in modified]
    change = ChangeSet(segments=new, modified=modified, unchanged=unchanged)
    return EvalEvent(
        eid, "P1", old, change, meta_before={"deadline": before}, meta_after={"deadline": after}
    )


@pytest.fixture
def chain():
    return Chain(
        "P1",
        V1,
        [
            event("e1", V1, V2, ["s1"], "2026-11-07", "2026-11-14"),
            event("e2", V2, V3, ["s2"], "2026-11-14", "2026-11-14"),
            event("e3", V3, V4, ["s2"], "2026-11-14", "2026-11-14"),
        ],
    )


LABELS = {
    ("e1", "deadline"): Label("changed", "api_meta"),
    ("e1", "amount"): Label("unchanged"),
    ("e2", "amount"): Label("wording_only"),
    ("e2", "deadline"): Label("unchanged"),
    ("e3", "amount"): Label("changed", "attachment"),
    ("e3", "deadline"): Label("unchanged"),
}
METHODS = {
    "B0": lambda: B0Meta(SPEC),
    "B2": lambda: B2Full(fake_extractor, SPEC),
    "엔진": lambda: Engine(fake_extractor, SPEC),
}


def test_methods_agree_on_real_changes_and_differ_in_cost(chain):
    b0, b2, engine = evaluate([chain], METHODS, LABELS)
    assert b0.predictions == {"e1": {"deadline"}, "e2": set(), "e3": set()}
    assert (
        b2.predictions == engine.predictions == {"e1": {"deadline"}, "e2": set(), "e3": {"amount"}}
    )
    # B0는 메타로 보이는 변경만 잡는다. 첨부에서만 보이는 변경은 놓친다
    assert (b0.score.tp, b0.score.fn) == (1, 1)
    assert b0.score_meta.recall() == 1.0 and b0.score_text.recall() == 0.0
    # 엔진과 B2는 둘 다 잡고, 문구만 바뀐 정정에는 알리지 않는다
    for r in (b2, engine):
        assert r.score.recall() == 1.0 and r.score.false_alert_rate() == 0.0 and r.judged == 3
    # 엔진은 바뀐 조각과 기존 근거만 넘긴다
    assert b0.calls == 0 and b2.calls == 3 and engine.calls == 3
    assert engine.input_chars < b2.input_chars


def test_without_cassettes_nothing_is_judged_or_scored(chain):
    def no_cassette(fields, segments):
        raise MissingCassette("k")

    (row,) = evaluate([chain], {"엔진": lambda: Engine(no_cassette, SPEC)}, LABELS)
    assert row.judged == 0 and row.score.labeled == 0 and row.calls == 0
    assert "라벨 없음" in render([row])


def test_b3_trusts_the_llm_answer_about_what_changed(tmp_path, chain):
    seen = []

    class DiffProvider:
        def complete(self, req):
            seen.append(req.input)
            answer = {"name": "amount", "changed": True, "value_json": "50000000"}
            return LLMResponse({"fields": [answer]}, 1, 1, 0.0)

    gateway = Gateway(DiffProvider(), FileCassetteStore(tmp_path), Mode.LIVE)
    (row,) = evaluate([chain], {"B3": lambda: B3Diff(fake_extractor, gateway, SPEC)}, LABELS)
    # 가짜 LLM이 매번 "금액이 바뀌었다"고 답한다. B3는 그대로 믿으므로 문구만 바뀐 e2에서 가짜 알림이 난다
    assert row.predictions == {"e1": {"amount"}, "e2": {"amount"}, "e3": {"amount"}}
    assert row.score.fp == 2 and row.calls == 3
    assert (
        "[s1] 이전: 신청 마감 2026-11-07" in seen[0]
        and "[s1] 이후: 신청 마감 2026-11-14" in seen[0]
    )
    assert '"2026-11-07"' in seen[0]  # 정정 전 값도 함께 준다


def test_score_counts_only_judged_events_and_splits_by_where_it_shows():
    predictions = {"e1": {"deadline"}, "e2": {"amount"}, "e3": None}
    s = score(predictions, LABELS)
    assert (s.tp, s.fn, s.fp, s.tn) == (1, 0, 1, 2)  # e3은 판단하지 못해 세지 않는다
    assert s.precision() == 0.5 and s.false_alert_rate() == pytest.approx(1 / 3)
    assert score(predictions, LABELS, visible_in=("attachment", "body")).recall() is None


def test_wilson_interval():
    assert wilson(0, 0) == (0.0, 1.0)
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.237, abs=0.001) and hi == pytest.approx(0.763, abs=0.001)
    assert wilson(10, 10)[1] == 1.0 and wilson(10, 10)[0] > 0.7


def test_split_is_stable_and_keeps_a_program_on_one_side():
    sides = {split_of(f"program-{i}") for i in range(50)}
    assert sides == {"dev", "test"} and split_of("program-1") == split_of("program-1")


def test_disagreements_and_input_sizes(chain):
    b0, b2, _ = evaluate([chain], METHODS, LABELS)
    assert disagreements(b0, b2) == [("e3", "amount")]
    sizes = input_sizes([chain])
    assert sizes[0] == {
        "b2": sum(len(s.text) for s in V2),
        "b3": len(V1[0].text) + len(V2[0].text),
        "engine_min": len(V2[0].text),
    }
    assert "B2 대비" in render([b0, b2], sizes)


def test_api_meta_reads_the_structured_deadline():
    raw = {"pblancId": "P1", "pblancNm": "(가상)", "reqstBeginEndDe": "2026-10-13 ~ 2026-11-07"}
    assert api_meta("bizinfo", raw) == {"deadline": "2026-11-07", "apply_start": "2026-10-13"}
    assert api_meta("unknown", raw) == {}


def test_chains_and_labels_load_from_recorded_events():
    engine = sqlite_engine()
    models.Base.metadata.create_all(engine)

    def raw(deadline):
        return {
            "pblancId": "X1",
            "pblancNm": "(가상)",
            "reqstBeginEndDe": f"2026-10-13 ~ {deadline}",
        }

    with Session(engine) as s:
        s.add(models.Source(id="bizinfo", name="기업마당"))
        s.add(
            models.Program(id="P1", title="(가상) 사업", title_key="가상사업", current_version_no=2)
        )
        s.flush()
        s.add(
            models.Notice(
                id="N1", source_id="bizinfo", external_id="X1", title="(가상)", program_id="P1"
            )
        )
        s.flush()
        for no, deadline, texts in ((1, "2026-11-07", V1), (2, "2026-11-14", V2)):
            nv = f"NV{no}"
            s.add(
                models.NoticeVersion(
                    id=nv,
                    notice_id="N1",
                    version_no=no,
                    kind="original",
                    content_hash=f"c{no}",
                    raw=raw(deadline),
                )
            )
            s.flush()
            s.add(
                models.ProgramVersion(
                    id=f"PV{no}",
                    program_id="P1",
                    version_no=no,
                    notice_version_id=nv,
                    content_hash=f"c{no}",
                )
            )
            for sg in texts:
                s.add(
                    models.SegmentRow(
                        notice_version_id=nv,
                        lineage_id=sg.lineage_id,
                        kind=sg.kind,
                        ord=sg.order,
                        text=sg.text,
                        text_hash=sg.text_hash,
                        locator={},
                    )
                )
        s.flush()
        empty = {"added": [], "removed": [], "modified": [], "unknown": [], "unchanged": 0}
        s.add(
            models.ChangeEvent(
                program_id="P1",
                from_version_id=None,
                to_version_id="PV1",
                changed_lineages={**empty, "added": ["s1", "s2", "s3"]},
            )
        )
        s.add(
            models.ChangeEvent(
                program_id="P1",
                from_version_id="PV1",
                to_version_id="PV2",
                changed_lineages={**empty, "modified": ["s1"]},
            )
        )
        s.add(
            models.LabelRow(
                program_id="P1",
                to_version_id="PV2",
                field="deadline",
                status="changed",
                visible_in="api_meta",
            )
        )
        s.commit()

        (chain,) = load_chains(s, split=split_of("P1"), meta=api_meta)
        assert load_chains(s, split="dev" if split_of("P1") == "test" else "test") == []
        labels = load_labels(s)
    assert [x.text for x in chain.first_segments] == [x.text for x in V1]
    (ev,) = chain.events
    assert ev.event_id == "PV2" and ev.change.modified == ["s1"]
    assert (ev.meta_before["deadline"], ev.meta_after["deadline"]) == ("2026-11-07", "2026-11-14")
    assert labels == {("PV2", "deadline"): Label("changed", "api_meta")}
    (row,) = evaluate([chain], {"B0": lambda: B0Meta(SPEC)}, labels)
    assert row.score.recall() == 1.0 and json.dumps(sa.inspect(engine).get_table_names())


def test_a_failed_call_stops_only_that_program(tmp_path):
    from noticedelta.evaluation.methods import CALL_FAILED
    from noticedelta.llm import ProviderError

    def broken(fields, segments):
        raise ProviderError("504 upstream_timeout")

    spec = Fields({"deadline": "datetime"}, {"deadline": "마감"})
    m = B2Full(broken, spec)
    m.fields = {}
    out = m.step(SimpleNamespace(change=SimpleNamespace(segments=[SimpleNamespace(text="x")])))
    assert out.changed is None and out.note == CALL_FAILED
