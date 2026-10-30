"""정답 라벨: 원문 비교, 저장, 2차 표본, 일치도, 라벨 화면 (작업 목록 5a)."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.labeling.store import (
    agreement,
    cohen_kappa,
    event_view,
    events,
    save_labels,
    second_round,
)

T0 = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'l.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    store = LocalBlobStore(tmp_path / "blobs")
    a = store.put(b"%PDF-1.7 old").key
    b = store.put(b"%PDF-1.7 new").key
    with factory() as s:
        s.add(models.Source(id="bizinfo", name="기업마당"))
        s.flush()
        for i in range(10):
            s.add(models.Program(id=f"PG{i}", title=f"(가상) 사업 {i}", title_key=f"k{i}"))
            s.add(models.Notice(id=f"N{i}", source_id="bizinfo", external_id=f"X{i}", title="t"))
            s.flush()
            for v, (deadline, body, blob) in enumerate(
                [
                    ("2026-11-07", "접수 마감 11월 7일\n문의 051-000-0000", a),
                    ("2026-11-14", "접수 마감 11월 14일\n문의 051-000-0000", b),
                ],
                start=1,
            ):
                s.add(
                    models.NoticeVersion(
                        id=f"NV{i}{v}", notice_id=f"N{i}", version_no=v, kind="original",
                        content_hash=f"h{i}{v}", body=body,
                        raw={"pblancId": f"X{i}", "reqstEndDe": deadline, "inqireCo": v * 10},
                    )
                )  # fmt: skip
                s.flush()
                s.add(
                    models.Attachment(
                        notice_version_id=f"NV{i}{v}",
                        filename="공고문.pdf",
                        url="u",
                        lineage_key="notice:공고문:0",
                        blob_key=blob,
                    )
                )
                s.add(models.ProgramVersion(id=f"PV{i}{v}", program_id=f"PG{i}", version_no=v, notice_version_id=f"NV{i}{v}", content_hash=f"h{i}{v}"))  # fmt: skip
                s.flush()
        s.commit()
    return factory, store


def label_all(s, ref, round, status_of, now):
    rows = {
        f: {"status": status_of(f)}
        for f in ("deadline", "amount", "eligibility_rules", "required_documents", "contact")
    }
    return save_labels(s, ref, round=round, labeler="me", rows=rows, now=now)  # fmt: skip


def test_events_and_raw_diff_without_the_engine(env):
    factory, _ = env
    with factory() as s:
        assert len(events(s)) == 10  # 사업마다 v1 → v2
        v = event_view(s, "PV02")
        changed = [r for r in v.api_rows if r[3]]
        assert [r[0] for r in changed] == ["reqstEndDe"]  # 조회수(inqireCo)는 비교에서 뺀다
        assert ("-", "접수 마감 11월 7일") in v.body_rows and (
            "+",
            "접수 마감 11월 14일",
        ) in v.body_rows
        assert ("=", "문의 051-000-0000") in v.body_rows
        assert v.attachments[0]["state"] == "바뀜"
        assert event_view(s, "PV01") is None  # 첫 버전은 정정이 아니다


def test_save_overwrites_same_round_and_skips_unanswered(env):
    factory, _ = env
    with factory() as s:
        ref = events(s)[0]
        assert save_labels(s, ref, round=1, labeler="me", rows={"deadline": {"status": "changed", "before": " 2026-11-07 ", "after": "2026-11-14", "visible_in": "api_meta"}}, now=T0) == 1  # fmt: skip
        save_labels(
            s, ref, round=1, labeler="me", rows={"deadline": {"status": "wording_only"}}, now=T0
        )
        rows = s.scalars(sa.select(models.LabelRow)).all()
        assert len(rows) == 1 and rows[0].status == "wording_only" and rows[0].before is None


def test_second_round_is_30_percent_after_7_days_and_stable(env):
    factory, _ = env
    with factory() as s:
        for e in events(s):
            label_all(s, e, 1, lambda f: "changed", T0)
        s.commit()
        assert second_round(s, T0 + timedelta(days=6)) == []
        due = second_round(s, T0 + timedelta(days=7))
        assert len(due) == 3  # 10건의 30%
        again = second_round(s, T0 + timedelta(days=8))
        assert [e.to_version.id for e in due] == [e.to_version.id for e in again]


def test_agreement_and_kappa(env):
    factory, _ = env
    with factory() as s:
        e1, e2 = events(s)[:2]
        label_all(s, e1, 1, lambda f: "changed" if f == "deadline" else "absent", T0)
        label_all(s, e2, 1, lambda f: "unchanged", T0)
        label_all(s, e1, 2, lambda f: "changed" if f == "deadline" else "absent", T0)
        label_all(s, e2, 2, lambda f: "wording_only" if f == "contact" else "unchanged", T0)
        a = agreement(s)
    assert a.pairs == 10 and a.agree == pytest.approx(0.9)
    assert 0 < a.kappa < 1


def test_cohen_kappa_edges():
    assert cohen_kappa(["a", "b"], ["a", "b"]) == 1.0
    assert cohen_kappa(["a", "a"], ["a", "a"]) == 1.0
    assert cohen_kappa([], []) is None


def test_label_screen_round_trip(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from noticedelta.labeling.app import create_label_app

    factory, store = env
    client = TestClient(create_label_app(factory, store, now=lambda: T0), follow_redirects=False)
    assert "(가상) 사업 0" in client.get("/").text
    page = client.get("/e/PV02?round=1").text
    assert "reqstEndDe" in page and "접수 마감 11월 14일" in page and "공고문.pdf" in page
    form = {f"{f}.status": "unchanged" for f in ("deadline", "amount", "eligibility_rules", "required_documents", "contact")}  # fmt: skip
    form |= {
        "deadline.status": "changed",
        "deadline.before": "2026-11-07",
        "deadline.after": "2026-11-14",
    }
    assert client.post("/e/PV02?round=1", data=form).status_code == 303
    assert 'value="2026-11-14"' in client.get("/e/PV02?round=1").text
    assert (
        'value="2026-11-14"' not in client.get("/e/PV02?round=2").text
    )  # 2차에서는 1차 답이 안 보인다
    blob = client.get(f"/blob/{store.put(b'%PDF-1.7 new').key}?name=공고문.pdf")
    assert blob.status_code == 200 and blob.content == b"%PDF-1.7 new"
    assert client.get("/blob/not-a-key").status_code == 400
    assert client.get("/e/nope").status_code == 404
