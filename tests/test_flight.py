"""비행 기록기와 같게 재생 (platform-services 3장)."""

import sqlalchemy as sa
from test_gateway import CountingProvider, req
from test_history import DOCS, RULES, pdf
from test_pipeline import Env

from noticedelta import flight
from noticedelta.db.models import Attachment, NoticeVersion, ParseCache, RecordRow, TraceRow
from noticedelta.llm import FileCassetteStore, Gateway, Mode
from noticedelta.replay import compare, replay


def live(tmp_path):
    """수집 두 번을 워커로 처리하고 묶음을 닫는다(운영과 같은 길)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = Env(tmp_path)
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        flight.close_finished(s, env.clock())
        s.commit()
    return env


def test_each_collect_run_is_a_closed_trace_with_parser_records(tmp_path):
    env = live(tmp_path)
    with env.factory() as s:
        traces = s.scalars(sa.select(TraceRow).order_by(TraceRow.name)).all()
        assert len(traces) == 2
        assert all(t.kind == "collect_run" and t.status == "closed" and t.digest for t in traces)
        assert traces[0].config["parser"] == "ByContent" and traces[0].platform_rev
        assert traces[0].events == 4  # 실행 끝, 새 판, 파싱, 첫 판의 변경 기록
        records = s.scalars(sa.select(RecordRow)).all()
        # 첨부 내용이 바뀌어 두 실행 모두 파서를 불렀다. 내용은 저장소에 있고 키와 해시만 남는다
        assert {r.kind for r in records} == {"parse"}
        assert {r.trace_id for r in records} == {t.id for t in traces}
        assert all(r.ref.startswith("blob:") and len(r.digest) == 64 for r in records)


def test_replay_from_scratch_matches_the_recorded_digest(tmp_path):
    env = live(tmp_path / "live")
    r = replay(env.factory, env.root, env.ctx.store, tmp_path / "replay")
    assert r.missing == [] and r.jobs == {"done": 6}
    assert len(r.runs) == 2
    for run in r.runs:
        assert run.comparable and run.same is True
        assert run.trace_id != run.recorded.id  # 새 DB라 ID는 모두 다르다
    with env.factory() as s:
        assert flight.canonical_lines(s, r.runs[1].recorded.id) == r.runs[1].lines
    assert any('"type": "change.detected"' in line for line in r.runs[1].lines)
    # 재생은 원문 저장소에 새로 쓰지 않는다
    again = replay(env.factory, env.root, env.ctx.store, tmp_path / "replay2")
    assert [x.digest for x in again.runs] == [x.digest for x in r.runs]


def test_replay_up_to_a_run_and_a_changed_input_shows_up(tmp_path):
    env = live(tmp_path / "live")
    first = replay(env.factory, env.root, env.ctx.store, tmp_path / "a", upto=env_run(env, 0))
    assert [x.run for x in first.runs] == [env_run(env, 0)]
    # 바꿔 재생: 두 번째 판의 첨부 파싱 결과를 첫 판 것으로 바꿔 넣으면 변경이 사라진다
    with env.factory() as s:
        v1 = s.scalar(
            sa.select(Attachment.parsed_key)
            .join(NoticeVersion, NoticeVersion.id == Attachment.notice_version_id)
            .where(NoticeVersion.version_no == 1)
        )
        s.execute(sa.update(ParseCache).values(parsed_key=v1))
        s.execute(sa.update(Attachment).values(parsed_key=v1))
        s.commit()
    changed = replay(env.factory, env.root, env.ctx.store, tmp_path / "b")
    assert changed.runs[0].same is True and changed.runs[1].same is False
    only_recorded, only_replay = compare(changed.runs[1].recorded_lines, changed.runs[1].lines)
    assert any("change.detected" in line for line in only_recorded)


def test_missing_parser_record_stops_with_a_reason(tmp_path):
    env = live(tmp_path / "live")
    with env.factory() as s:
        s.execute(sa.delete(ParseCache))
        s.execute(sa.update(Attachment).values(parsed_key=None))
        s.commit()
    r = replay(env.factory, env.root, env.ctx.store, tmp_path / "replay")
    assert r.missing and r.missing[0].startswith("MissingRecording: 파싱 기록 없음")
    assert r.jobs.get("dead") == len(r.missing)


def test_llm_calls_are_recorded_only_inside_a_bound_trace(tmp_path):
    env = Env(tmp_path)
    gw = Gateway(
        CountingProvider(),
        FileCassetteStore(tmp_path / "cassettes"),
        Mode.RECORD,
        observer=flight.llm_observer,
    )
    with env.factory() as s:
        flight.open_trace(s, "T" * 26, kind="eval", started_at=env.clock(), config={"model": "m"})
        gw.complete(req("outside"))  # 묶음 밖: 남지 않는다
        with flight.bound(s, "T" * 26, env.clock):
            gw.complete(req("inside"))
        rows = s.scalars(sa.select(RecordRow)).all()
    assert len(rows) == 1
    assert rows[0].kind == "llm_call" and rows[0].ref.startswith("cassette:")
    assert rows[0].cost == 110 and rows[0].payload["from_cassette"] is False


def env_run(env, i):
    return sorted(p.stem for p in (env.root / "runs").glob("*.json"))[i]
