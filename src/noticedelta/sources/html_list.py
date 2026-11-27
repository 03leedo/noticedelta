"""게시판(글 목록 페이지) 커넥터 (source-catalog.md 3장의 `html_list`): 사용자가 등록하는 두 번째 출처 종류.

    cands = candidates(html, base_url)        # 플랫폼이 되풀이되는 글 목록의 위치를 찾아 제안한다
    items = extract(html, base_url, cands[0]["list"])   # 고른 위치로 글(원본 사전)을 뽑는다

- 목록 위치(list)는 글 링크(<a href>)까지의 조상 모양(태그와 class)을 ">"로 이은 것이다. 에이전트가 CSS
  선택자를 쓰지 않아도, 플랫폼이 낸 후보에서 고른다. 같은 모양의 링크가 많이 되풀이되는 곳이 글 목록이다.
- 글마다 제목(링크 글), 주소(절대 주소), 날짜(그 줄의 글에서 찾은 YYYY-MM-DD 꼴)를 뽑는다.
- 받기·안전 검사는 RSS와 같다(sources.rss.fetch_bytes: 사설 주소, 넘겨주기, 크기·시간). 자바스크립트로
  그리는 페이지와 로그인이 필요한 페이지는 받지 않는다(1차).
- 받은 글은 신뢰할 수 없는 입력(A)이다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

from noticedelta.sources.base import NoticeRecord, SourceError

MIN_ITEMS = 3  # 이보다 적게 되풀이되면 글 목록으로 보지 않는다
MAX_ITEMS = 200
ROWS = ("tr", "li", "article", "dd", "div")  # 글 한 줄로 보는 조상(가까운 것부터)
_DATE = re.compile(r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})")
_SKIP = {"script", "style", "noscript", "template"}
_VOID = {
    "br",
    "img",
    "hr",
    "input",
    "meta",
    "link",
    "area",
    "base",
    "col",
    "embed",
    "source",
    "wbr",
}
KEYS = {
    "guid": "글을 가리키는 값(글 주소)",
    "title": "글 제목(목록의 링크 글)",
    "link": "글 주소",
    "date": "목록에 적힌 날짜(YYYY-MM-DD로 맞춘 것, 없으면 빈 값)",
}


@dataclass
class Node:
    tag: str
    attrs: dict
    parent: Node | None = None
    children: list = field(default_factory=list)  # Node 또는 글(str)

    def text(self) -> str:
        parts = []
        for c in self.children:
            parts.append(c if isinstance(c, str) else c.text())
        return " ".join(" ".join(parts).split())

    def shape(self) -> str:
        classes = ".".join(sorted((self.attrs.get("class") or "").split()))
        return f"{self.tag}.{classes}" if classes else self.tag


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        node = Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        node = self.cur
        while node is not self.root and node.tag != tag:  # 닫지 않은 태그는 함께 닫는다
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.cur.children.append(data)


def _tree(html: str) -> Node:
    t = _Tree()
    t.feed(html)
    return t.root


def _anchors(node: Node):
    for c in node.children:
        if isinstance(c, Node):
            if c.tag == "a" and c.attrs.get("href"):
                yield c
            yield from _anchors(c)


def _path(a: Node) -> str:
    shapes, n = [], a
    while n is not None and n.tag not in ("#root", "html", "body"):
        shapes.append(n.shape())
        n = n.parent
    return ">".join(reversed(shapes))


def _row(a: Node) -> Node:
    n = a.parent
    while n is not None and n.tag != "#root":
        if n.tag in ROWS:
            return n
        n = n.parent
    return a.parent or a


def _date(text: str) -> str:
    m = _DATE.search(text)
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else ""


def _item(a: Node, base: str) -> dict | None:
    href = a.attrs.get("href", "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    link = urldefrag(urljoin(base, href))[0]
    title = a.text()
    if len(title) < 2:
        return None
    return {"guid": link, "title": title[:500], "link": link, "date": _date(_row(a).text())}


def decode(data: bytes) -> str:
    """받은 바이트를 글로. UTF-8이 아니면 한국 공공 사이트에 흔한 CP949(EUC-KR)로 읽는다."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def candidates(html: str, base: str) -> list[dict]:
    """글 목록으로 보이는 곳들(많이 되풀이되는 순). 후보마다 위치(list), 글 수, 앞의 글 몇 개."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in _anchors(_tree(html)):
        item = _item(a, base)
        if item is not None:
            groups[_path(a)].append(item)
    out = []
    for path, items in groups.items():
        unique = list({it["link"]: it for it in items}.values())
        if len(unique) < MIN_ITEMS:
            continue
        dated = sum(1 for it in unique if it["date"])
        # 날짜가 붙은 줄이 많고 제목이 긴 묶음일수록 글 목록이다(메뉴·쪽 번호와 가르려고)
        score = (
            len(unique)
            * (1 + dated / len(unique))
            * min(1.0, sum(len(it["title"]) for it in unique) / len(unique) / 8)
        )
        out.append({"list": path, "count": len(unique), "dated": dated, "score": round(score, 2),
                    "samples": unique[:5]})  # fmt: skip
    return sorted(out, key=lambda c: -c["score"])


def extract(html: str, base: str, list_path: str) -> list[dict]:
    """고른 위치(list)의 글(원본 사전). 같은 주소는 한 번만."""
    seen: dict[str, dict] = {}
    for a in _anchors(_tree(html)):
        if _path(a) != list_path:
            continue
        item = _item(a, base)
        if item is not None and item["link"] not in seen:
            seen[item["link"]] = item
    return list(seen.values())[:MAX_ITEMS]


def to_record(source_id: str) -> Callable[[dict], NoticeRecord]:
    """게시판 글(원본 사전)을 NoticeRecord로. 글 본문은 받지 않는다(1차: 목록의 제목·날짜가 바뀌면 변경)."""
    from noticedelta.sources.rss import to_record as feed_record

    feed = feed_record(source_id)

    def convert(raw: dict) -> NoticeRecord:
        if not raw.get("link"):
            raise SourceError("글 주소가 없다")
        rec = feed({"guid": raw["link"], "title": raw.get("title"), "link": raw["link"],
                    "description": raw.get("date") or ""})  # fmt: skip
        return replace(rec, raw={k: raw.get(k) for k in KEYS})

    return convert
