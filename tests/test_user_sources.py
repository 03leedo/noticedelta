"""사용자 출처(RSS): 플랫폼이 제안하고, 사람이 확인하고, 소유자의 앱만 쓴다 (source-catalog.md 4·6장)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from noticedelta import programs, user_sources
from noticedelta.app_tools import app_tools
from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.grants import APP
from noticedelta.snapshot import run_snapshot
from noticedelta.source_tools import source_tools
from noticedelta.sources import rss
from noticedelta.sources.base import SourceError
from noticedelta.tool_gateway import ToolGateway

T0 = datetime(2026, 11, 27, 9, tzinfo=UTC)
FEED = "https://dept.example.ac.kr/notice/rss"
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>학과 공지</title>
<item><title>2027 장학금 신청 안내</title><link>https://dept.example.ac.kr/n/2</link>
<guid>n2</guid><description><![CDATA[<p>신청 <b>마감</b> 12월 5일</p>]]></description>
<pubDate>Thu, 26 Nov 2026 09:00:00 +0900</pubDate></item>
<item><title>겨울 계절학기</title><link>https://dept.example.ac.kr/n/1</link></item>
</channel></rss>""".encode()
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>blog</title>
<entry><id>tag:1</id><title>first</title><link rel="alternate" href="https://b.example/1"/>
<summary>hello &amp; bye</summary><updated>2026-11-27T00:00:00Z</updated></entry></feed>"""


def test_feeds_are_read_and_dangerous_xml_is_refused():
    title, items = rss.parse_feed(RSS)
    assert title == "학과 공지" and len(items) == 2
    assert items[0]["description"] == "신청 마감 12월 5일"  # 태그를 지운 글
    assert items[1]["guid"] == "https://dept.example.ac.kr/n/1"  # guid가 없으면 주소
    _, atom = rss.parse_feed(ATOM)
    assert atom[0] == {"guid": "tag:1", "title": "first", "link": "https://b.example/1",
                       "description": "hello & bye", "published": "2026-11-27T00:00:00Z"}  # fmt: skip
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>'
    with pytest.raises(SourceError, match="DOCTYPE"):
        rss.parse_feed(bomb)
    rec = rss.to_record("u-feed")({"guid": "x" * 300, "title": "t", "link": "https://a/1"})
    assert rec.external_id.startswith("h:") and len(rec.external_id) <= 100


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/rss",
        "http://10.0.0.5/rss",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com:8443/rss",
        "file:///etc/passwd",
        "ftp://example.com/rss",
        "http://localhost/rss",
    ],
)
def test_private_and_odd_addresses_are_refused(url):
    with pytest.raises(rss.UnsafeURL):
        rss.check_url(url)


def test_redirects_are_checked_again():
    def handler(request):
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "http://10.1.2.3/rss"})
        return httpx.Response(200, content=RSS)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    public = lambda host: not host.startswith("10.")  # noqa: E731 - 이름 풀이 대신
    with pytest.raises(rss.UnsafeURL):
        rss.fetch_bytes("https://good.example/rss", http, resolve=public)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'u.db'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    http = lambda: httpx.Client(transport=httpx.MockTransport(  # noqa: E731
        lambda r: httpx.Response(200, content=RSS)))  # fmt: skip
    tools = [*source_tools(factory, http=http, resolve=lambda h: True, now=lambda: T0),
             *app_tools(factory, preview=lambda d, o: {})]  # fmt: skip
    gate = ToolGateway(tools, chosen=[t.name for t in tools])
    with factory() as s:
        ids = {p: gate.open_session(s, app_id=APP.id, agent="a", person_id=p, now=T0).id
               for p in ("p1", "p2")}  # fmt: skip
        s.commit()
    return factory, gate, ids


def call(env, who, name, **args):
    factory, gate, ids = env
    with factory() as s:
        return gate.call(s, ids[who], name, args, now=T0)


def register(env):
    factory, gate, _ = env
    probe = call(env, "p1", "probe_source", url=FEED)
    assert "글 2개를 찾았습니다" in probe["say"] and "2027 장학금" in probe["say"]
    draft = probe["draft"]
    out = call(env, "p1", "register_source", draft=draft)["action"]
    assert out["status"] == "held"  # 이용 조건 동의는 늘 사람이 확인한다
    with factory() as s:
        gate.approve(s, out["id"], by="person:p1", now=T0)
        s.commit()
        gate.commit_due(s, now=T0 + timedelta(minutes=10))
    return draft["id"]


def test_register_flow_and_only_the_owner_sees_and_uses_it(env):
    factory, _, _ = env
    sid = register(env)
    with factory() as s:
        row = s.get(models.UserSourceRow, sid)
        assert row.owner == "p1" and s.get(models.Source, sid) is not None
        terms = row.body["terms"]
        assert terms["agreed_by"] == "person:p1" and terms["agreed_at"].startswith("2026-11-27")
    assert sid in {x["id"] for x in call(env, "p1", "find_sources")["sources"]}
    assert sid not in {x["id"] for x in call(env, "p2", "find_sources")["sources"]}
    # 같은 피드를 다른 사람이 등록하면 겹치지 않는 id를 제안받는다(남의 출처가 있다는 말은 하지 않는다)
    second = call(env, "p2", "probe_source", url=FEED)["draft"]
    assert second["id"] != sid and second["id"].endswith("_2") and len(second["id"]) <= 32
    taken = call(env, "p2", "register_source", draft={**second, "id": sid})
    assert (
        "쓸 수 없다" in taken["blocked"]["reason"]
        and "find_sources" not in taken["blocked"]["reason"]
    )
    # 소유자의 앱만 그 출처를 쓴다
    app = APP.model_dump(mode="json", by_alias=True, exclude_none=True)
    app.update(id="my-feed-app", sources=[{"id": sid, "read": ["title", "description"]}])
    assert call(env, "p1", "check_app", definition=app)["ok"]
    other = call(env, "p2", "check_app", definition=app)
    assert not other["ok"] and any(sid in p["what"] for p in other["problems"])


def test_unsafe_or_non_rss_drafts_are_refused(env):
    probe = call(env, "p1", "probe_source", url="http://169.254.169.254/x")
    assert "받지 않는 주소" in probe["error"]
    draft = call(env, "p1", "probe_source", url=FEED)["draft"]
    code = {**draft, "connector": {"kind": "code", "adapter": "evil"}}
    assert "rss와 html_list" in call(env, "p1", "register_source", draft=code)["error"]
    fast = {**draft, "fetch": {"every_hours": 1, "rate_per_second": 5}}
    assert "초당" in call(env, "p1", "register_source", draft=fast)["error"]


def test_a_registered_feed_is_collected_and_loaded_as_its_own_items(env, tmp_path):
    factory, _, _ = env
    sid = register(env)
    root = tmp_path / "snap"
    convert = rss.to_record(sid)
    _, items = rss.parse_feed(RSS)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    run_snapshot({sid: lambda p: [convert(i) for i in items] if p == 1 else []},
                 http=http, root=root, now=lambda: datetime(2026, 11, 27, 10))  # fmt: skip
    [path] = (root / "runs").glob("*.json")
    with factory() as s:
        programs.load_run(s, path, LocalBlobStore(root / "blobs"), programs.LoadSummary())
        s.commit()
        notices = s.scalars(sa.select(models.Notice).where(models.Notice.source_id == sid)).all()
        assert {n.external_id for n in notices} == {"n2", "https://dept.example.ac.kr/n/1"}
        assert (
            len({n.item_id for n in notices}) == 2
        )  # 글 하나가 항목 하나(기관이 없어 묶지 않는다)
        assert user_sources.to_record_for(s, sid) is not None


def test_encoding_tricks_cgnat_and_rebinding_are_refused():
    sneaky = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "PWNED">]>'
              "<rss><channel><title>&a;</title></channel></rss>").encode("utf-16")  # fmt: skip
    with pytest.raises(SourceError, match="DOCTYPE"):
        rss.parse_feed(sneaky)
    for url in ("http://100.100.100.200/latest", "http://[::ffff:127.0.0.1]/x"):
        with pytest.raises(rss.UnsafeURL):
            rss.check_url(url)

    class Stream:
        def get_extra_info(self, key):
            return ("10.0.0.7", 443) if key == "server_addr" else None

    resp = httpx.Response(200, extensions={"network_stream": Stream()})
    with pytest.raises(rss.UnsafeURL, match="연결된 주소"):
        rss._peer_ok(resp)  # 이름이 검사 뒤에 사설 주소로 풀려 연결됐다


def test_items_without_ids_are_dropped_and_old_feed_items_stay_open(env, tmp_path):
    _, dropped = rss.parse_feed(
        b"<rss><channel><item><title>no link</title></item></channel></rss>"
    )
    assert dropped == []  # guid·link가 없는 글은 가리킬 수 없어 버린다
    _, items = rss.parse_feed(RSS)
    factory, _, _ = env
    sid = register(env)
    root = tmp_path / "snap"
    convert = rss.to_record(sid)
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    for hour, feed in ((10, items), (16, items[:1])):  # 두 번째에는 오래된 글이 피드에서 밀려났다
        run_snapshot({sid: lambda p, f=feed: [convert(i) for i in f] if p == 1 else []},
                     http=http, root=root, now=lambda h=hour: datetime(2026, 11, 27, h))  # fmt: skip
    with factory() as s:
        for path in sorted((root / "runs").glob("*.json")):
            programs.load_run(s, path, LocalBlobStore(root / "blobs"), programs.LoadSummary())
        s.commit()
        statuses = {n.external_id: n.status for n in s.scalars(sa.select(models.Notice))}
    assert set(statuses.values()) == {"open"}  # 피드에서 밀려났다고 닫지 않는다
