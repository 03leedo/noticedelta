"""게시판 출처(html_list): 플랫폼이 글 목록의 위치를 찾아 제안하고, 그 위치로 글을 뽑는다."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import programs
from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.grants import APP
from noticedelta.snapshot import run_snapshot
from noticedelta.source_tools import source_tools
from noticedelta.sources import html_list
from noticedelta.tool_gateway import ToolGateway

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)
URL = "https://cse.example.ac.kr/bbs/notice"
BOARD = """<html><head><title>학부 공지</title></head><body>
<nav><a href="/">홈</a> <a href="/about">소개</a> <a href="/bbs/notice">공지</a></nav>
<table class="board"><tbody>
<tr><td class="num">3</td><td class="subject"><a href="view?id=3">2027 겨울 계절학기 수강 신청 안내</a></td><td class="date">2026.11.26</td></tr>
<tr><td class="num">2</td><td class="subject"><a href="view?id=2">졸업 논문 심사 일정</a></td><td class="date">2026.11.20</td></tr>
<tr><td class="num">1</td><td class="subject"><a href="view?id=1#top">장학금 신청 안내</a></td><td class="date">2026-11-02</td></tr>
</tbody></table>
<div class="paging"><a href="?page=1">1</a><a href="?page=2">2</a><a href="?page=3">3</a></div>
<script>var x = '<a href="/evil">nope</a>';</script>
</body></html>"""


def test_the_board_list_is_found_ahead_of_menus_and_paging():
    found = html_list.candidates(BOARD, URL)
    best = found[0]
    assert best["list"] == "table.board>tbody>tr>td.subject>a" and best["count"] == 3
    assert best["samples"][0] == {"guid": "https://cse.example.ac.kr/bbs/view?id=3",
                                  "title": "2027 겨울 계절학기 수강 신청 안내",
                                  "link": "https://cse.example.ac.kr/bbs/view?id=3",
                                  "date": "2026-11-26"}  # fmt: skip
    items = html_list.extract(BOARD, URL, best["list"])
    assert [it["link"][-4:] for it in items] == ["id=3", "id=2", "id=1"]  # 조각(#top)은 뗀다
    assert items[2]["date"] == "2026-11-02"
    assert all("evil" not in it["link"] for c in found for it in c["samples"])  # 스크립트 안은 무시
    assert html_list.decode("공지".encode("cp949")) == "공지"  # EUC-KR 사이트


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'h.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    http = lambda: httpx.Client(transport=httpx.MockTransport(  # noqa: E731
        lambda r: httpx.Response(200, content=BOARD.encode("cp949"))))  # fmt: skip
    tools = source_tools(factory, http=http, resolve=lambda h: True, now=lambda: T0)
    gate = ToolGateway(tools, chosen=[t.name for t in tools])
    with factory() as s:
        sid = gate.open_session(s, app_id=APP.id, agent="a", person_id="p1", now=T0).id
        s.commit()
    return factory, gate, sid


def test_probe_proposes_a_board_and_registration_loads_its_posts(env, tmp_path):
    factory, gate, sid = env
    with factory() as s:
        probe = gate.call(s, sid, "probe_source", {"url": URL}, now=T0)
    assert "게시판에서 글 3개를 찾았습니다" in probe["say"]
    draft = probe["draft"]
    assert draft["connector"] == {"kind": "html_list", "url": URL,
                                  "list": "table.board>tbody>tr>td.subject>a"}  # fmt: skip
    assert draft["title"] == "학부 공지"  # 메뉴·쪽 번호는 글 목록 후보가 아니다(짧은 글)
    with factory() as s:
        out = gate.call(s, sid, "register_source", {"draft": draft}, now=T0)["action"]
        gate.approve(s, out["id"], by="person:p1", now=T0)
        s.commit()
        gate.commit_due(s, now=T0 + timedelta(minutes=10))
    convert = html_list.to_record(draft["id"])
    items = html_list.extract(BOARD, URL, draft["connector"]["list"])
    root = tmp_path / "snap"
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    run_snapshot({draft["id"]: lambda p: [convert(i) for i in items] if p == 1 else []},
                 http=http, root=root, now=lambda: datetime(2026, 11, 27, 10))  # fmt: skip
    [path] = (root / "runs").glob("*.json")
    with factory() as s:
        programs.load_run(s, path, LocalBlobStore(root / "blobs"), programs.LoadSummary())
        s.commit()
        titles = s.scalars(
            sa.select(models.Notice.title).where(models.Notice.source_id == draft["id"])
        ).all()
    assert sorted(titles) == [
        "2027 겨울 계절학기 수강 신청 안내",
        "장학금 신청 안내",
        "졸업 논문 심사 일정",
    ]


def test_pages_with_no_list_are_refused(env):
    factory, gate, sid = env
    bare = "<html><body><p>공지가 없습니다</p><a href='/'>홈</a></body></html>"
    tools = source_tools(factory, resolve=lambda h: True, now=lambda: T0,
                         http=lambda: httpx.Client(transport=httpx.MockTransport(
                             lambda r: httpx.Response(200, content=bare.encode()))))  # fmt: skip
    g2 = ToolGateway(tools, chosen=[t.name for t in tools])
    with factory() as s:
        sid2 = g2.open_session(s, app_id=APP.id, agent="b", person_id="p2", now=T0).id
        out = g2.call(s, sid2, "probe_source", {"url": URL}, now=T0)
    assert "글 목록으로 보이는 곳도 찾지 못했다" in out["error"]


def test_real_world_board_shapes():
    # 줄무늬 class(li.odd/li.even)와 고정 공지(tr.notice)가 섞여도 한 목록이다
    striped = (
        "<ul class='list'>"
        + "".join(
            f"<li class='{'odd' if i % 2 else 'even'}'><a href='v{i}'>공지 제목 {i}번</a></li>"
            for i in range(8)
        )
        + "</ul>"
    )
    [best] = html_list.candidates(striped, URL)
    assert best["count"] == 8 and best["list"] == "ul.list>li>a"
    # 끝 태그를 생략한 li
    loose = (
        "<ul>"
        + "".join(f"<li><a href='p{i}'>생략된 끝 태그 글 {i}</a>" for i in range(4))
        + "</ul>"
    )
    assert html_list.candidates(loose, URL)[0]["count"] == 4
    # 제목·날짜가 각각 div에 든 줄: 링크가 섞이기 직전까지의 조상이 줄이다
    divs = (
        "<ul>"
        + "".join(
            f"<li><div class=t><a href='d{i}'>2026.12.1까지 신청 {i}</a></div>"
            f"<div class=d>2026.11.0{i + 1}</div></li>"
            for i in range(4)
        )
        + "</ul>"
    )
    items = html_list.extract(divs, URL, html_list.candidates(divs, URL)[0]["list"])
    assert [it["date"] for it in items] == ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"]
    # 있을 수 없는 날짜는 버린다
    assert html_list._date("2026.13.45 등록") == ""


def test_odd_links_sessions_and_deep_pages_do_not_break_collection():
    rows = "".join(
        f"<tr><td><a href='view.do;jsessionid=ABC{i}?id={i}&page=3'>글 제목 {i}</a></td>"
        f"<td>2026-11-2{i}</td></tr>"
        for i in range(3)
    )
    page = f"<table>{rows}<tr><td><a href='JavaScript:go(1)'>자바스크립트 글</a></td></tr></table>"
    items = html_list.extract(page, URL, html_list.candidates(page, URL)[0]["list"])
    assert [it["guid"] for it in items] == [
        f"https://cse.example.ac.kr/bbs/view.do?id={i}" for i in range(3)
    ]  # 세션값·쪽 번호를 뗀다
    assert all(it["link"].startswith("https://") for it in items)  # javascript: 는 받지 않는다
    deep = "<div>" + "<option>" * 1500 + "<a href='x'>깊은 글</a>"
    assert html_list.candidates(deep, URL) == []  # 재귀 오류 없이 끝난다
