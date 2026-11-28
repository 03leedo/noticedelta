"""파일 올리기 출처(file_drop): 사람이 목록 파일의 새 판을 올리고, 판 사이의 변경을 찾는다 (source-catalog.md 3장)."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import people, programs, user_sources
from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.grants import APP
from noticedelta.snapshot import run_snapshot
from noticedelta.source_tools import source_tools
from noticedelta.sources import file_drop
from noticedelta.sources.base import SourceError
from noticedelta.tool_gateway import ToolGateway

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)
V1 = "번호,사업명,마감,링크\n1,청년 창업 지원,2026-12-10,https://org.example/1\n2,수출 바우처,2026-12-20,\n3,R&D 과제,2027-01-05,\n"
V2 = "번호,사업명,마감,링크\n1,청년 창업 지원,2026-12-15,https://org.example/1\n3,R&D 과제,2027-01-05,\n4,판로 개척,2027-01-10,\n"


def test_files_are_read_as_rows_with_limits():
    rows = file_drop.parse(("﻿" + V1).encode("utf-8"), "csv")
    assert rows[0] == {"번호": "1", "사업명": "청년 창업 지원", "마감": "2026-12-10",
                       "링크": "https://org.example/1"}  # fmt: skip
    assert file_drop.parse(V1.encode("cp949"), "csv") == rows  # 엑셀이 낸 CP949도
    as_json = json.dumps({"items": [{"no": 1, "title": "가", "open": True, "x": None}]})
    assert file_drop.parse(as_json.encode(), "json") == [
        {"no": "1", "title": "가", "open": "true", "x": ""}]  # fmt: skip
    for bad, fmt, why in (
        (b'[{"a": {"b": 1}}]', "json", "객체나 배열"),
        (b'{"a": 1}', "json", "객체의 배열"),
        (b"a,a\n1,2\n", "csv", "같은 이름"),
        (b"a,b\n,\n", "csv", "줄이 하나도"),
        (b"x" * (file_drop.MAX_BYTES + 1), "csv", "너무 크다"),
    ):
        with pytest.raises(SourceError, match=why):
            file_drop.parse(bad, fmt)
    assert file_drop.format_of("list.bin", b'  [{"a":1}]') == "json"


def test_the_platform_proposes_id_title_and_link_columns():
    rows = file_drop.parse(V1.encode(), "csv")
    assert file_drop.propose(rows) == {"id": "번호", "title": "사업명", "url": "링크"}
    assert file_drop.id_problem(rows + [dict(rows[0])], "번호").endswith("달라야 한다)")
    no_id = file_drop.parse(b"kind,title\na,x\na,y\n", "csv")
    assert file_drop.propose(no_id)["id"] == "title"  # 겹치지 않는 칸을 고른다


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'f.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    tools = source_tools(factory, http=lambda: None, resolve=lambda h: True, now=lambda: T0)
    gate = ToolGateway(tools, chosen=[t.name for t in tools])
    with factory() as s:
        ids = {}
        for p in ("p1", "p2"):
            ids[p] = gate.open_session(s, app_id=APP.id, agent="a", person_id=p, now=T0).id
        s.commit()
    return factory, gate, ids


def call(env, who, name, **args):
    factory, gate, ids = env
    with factory() as s:
        return gate.call(s, ids[who], name, args, now=T0)


def register(env) -> str:
    factory, gate, _ = env
    probe = call(
        env, "p1", "probe_file", content=V1, filename="programs.csv", title="기관 사업 목록"
    )
    assert "3줄" in probe["say"] and "「번호」" in probe["say"]
    draft = probe["draft"]
    assert (
        draft["connector"] == {"kind": "file_drop", "format": "csv"} and draft["id"] == "f_programs"
    )
    out = call(env, "p1", "register_source", draft=draft)["action"]
    assert out["status"] == "held"  # 등록은 늘 사람이 확인한다
    with factory() as s:
        gate.approve(s, out["id"], by="person:p1", now=T0)
        s.commit()
        gate.commit_due(s, now=T0 + timedelta(minutes=10))
        assert s.get(models.Source, draft["id"]).base_url is None
    return draft["id"]


def test_only_the_owner_drops_editions_and_undo_withdraws(env):
    factory, gate, _ = env
    sid = register(env)
    first = call(env, "p1", "drop_file", source_id=sid, content=V1, filename="v1.csv")
    assert first["edition"] == 1 and first["rows"] == 3 and first["new"]
    assert call(env, "p1", "drop_file", source_id=sid, content=V1)["new"] is False  # 같은 파일
    assert (
        "내가 등록한"
        in call(env, "p2", "drop_file", source_id=sid, content=V2)["blocked"]["reason"]
    )
    dup = V2 + "4,또 판로,2027-01-11,\n"
    assert (
        "겹치는 값" in call(env, "p1", "drop_file", source_id=sid, content=dup)["blocked"]["reason"]
    )
    second = call(env, "p1", "drop_file", source_id=sid, content=V2, filename="v2.csv")
    assert second["edition"] == 2
    with factory() as s:
        assert user_sources.current_drop(s, sid).seq == 2
        gate.undo(s, second["action"]["id"], by="person:p1", now=T0)
        assert user_sources.current_drop(s, sid).seq == 1  # 거두면 앞의 판을 읽는다


def test_editions_are_collected_and_changes_found_between_them(env, tmp_path):
    factory, _, _ = env
    sid = register(env)
    root = tmp_path / "snap"
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    def collect(hour: int) -> None:
        def page(n: int):
            with factory() as s:
                return user_sources.read_current(s, sid) if n == 1 else []

        run_snapshot({sid: page}, http=http, root=root, now=lambda: datetime(2026, 11, 27, hour))

    collect(10)  # 올린 판이 없으면 빈 목록
    for hour, text in ((11, V1), (12, V2)):
        with factory() as s:
            user_sources.drop(s, sid, text.encode(), f"{hour}.csv", by="p1", at=T0)
            s.commit()
        collect(hour)
    with factory() as s:
        for path in sorted((root / "runs").glob("*.json")):
            programs.load_run(s, path, LocalBlobStore(root / "blobs"), programs.LoadSummary())
        s.commit()
        notices = {n.external_id: n for n in s.scalars(sa.select(models.Notice))}
        assert {k: n.status for k, n in notices.items()} == {
            "1": "open",
            "2": "closed",
            "3": "open",
            "4": "open",
        }  # 판에서 빠진 줄은 닫는다
        versions = s.scalars(sa.select(models.NoticeVersion).where(
            models.NoticeVersion.notice_id == notices["1"].id)).all()  # fmt: skip
        assert len(versions) == 2  # 마감이 바뀌었다
        assert notices["1"].url == "https://org.example/1" and notices["2"].url is None


def test_forgetting_a_person_stops_their_sources_and_clears_files(env):
    factory, _, _ = env
    sid = register(env)
    call(env, "p1", "drop_file", source_id=sid, content=V1)
    with factory() as s:
        assert user_sources.forget(s, "p1", at=T0) == 2
        s.commit()
        assert sid not in user_sources.sheets(s)
        [row] = user_sources.drops(s, sid)
        assert row.body is None and row.withdrawn_at is not None and row.rows == 3


def test_upload_and_withdraw_on_the_web(env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from noticedelta.web.app import create_app
    from noticedelta.web.demo import seed_demo

    factory, _, _ = env
    with factory() as s:
        seed_demo(s, date(2026, 11, 27))
        p, token = people.create(s, at=T0)
        other, other_token = people.create(s, at=T0)
        for who in (p, other):
            s.add(models.Profile(person_id=who.id, attributes={}))
        s.commit()
        pid = p.id
    sheet = call(env, "p1", "probe_file", content=V1, filename="programs.csv")["draft"]
    sheet.update(owner=f"person:{pid}", id="f_web")
    with factory() as s:
        from noticedelta.catalog import SourceSheet

        user_sources.register(s, SourceSheet.model_validate(sheet), owner=pid, at=T0)
        s.commit()
    app = create_app(factory, today=lambda: date(2026, 11, 27), now=lambda: T0,
                     preview=lambda d, o: {})  # fmt: skip
    client = TestClient(app, follow_redirects=False)
    files = {"file": ("v1.csv", V1.encode("cp949"), "text/csv")}
    assert client.post(f"/p/{token}/sources/f_web/drop", files=files).status_code == 303
    page = client.get(f"/p/{token}/sources").text
    assert "f_web" in page and "v1.csv" in page and "거두기" in page
    bad = {"file": ("bad.csv", b"x,y\n1,2\n", "text/csv")}
    assert client.post(f"/p/{token}/sources/f_web/drop", files=bad).status_code == 400
    assert client.post(f"/p/{other_token}/sources/f_web/drop", files=files).status_code == 404
    with factory() as s:
        [d] = user_sources.drops(s, "f_web")
    assert client.post(f"/p/{other_token}/sources/drops/{d.id}/withdraw").status_code == 404
    assert client.post(f"/p/{token}/sources/drops/{d.id}/withdraw").status_code == 303
    assert "거둠" in client.get(f"/p/{token}/sources").text
