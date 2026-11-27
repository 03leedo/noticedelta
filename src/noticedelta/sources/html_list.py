"""게시판(글 목록 페이지) 커넥터 (source-catalog.md 3장의 `html_list`): 사용자가 등록하는 두 번째 출처 종류.

    cands = candidates(html, base_url)        # 플랫폼이 되풀이되는 글 목록의 위치를 찾아 제안한다
    items = extract(html, base_url, cands[0]["list"])   # 고른 위치로 글(원본 사전)을 뽑는다

- 목록 위치(list)는 글 링크(<a href>)까지의 조상 모양(태그와 class)을 ">"로 이은 것이다. 에이전트가 CSS
  선택자를 쓰지 않아도, 플랫폼이 낸 후보에서 고른다. 같은 모양의 링크가 많이 되풀이되는 곳이 글 목록이다.
  줄 요소(tr, li, dd, dt, article)의 class는 모양에 넣지 않는다(줄무늬·고정 공지·새 글 표시로 갈리지 않게).
- 글 한 줄은 다른 글의 링크가 섞이기 직전까지의 가장 높은 조상이다. 날짜는 그 줄에서 링크 밖의 글에서
  찾은 마지막 YYYY-MM-DD 꼴이다(제목 안의 날짜를 쓰지 않게, 있을 수 없는 날짜는 버린다).
- 글을 가리키는 값(guid)은 주소에서 세션값(;jsessionid, 세션 쿼리)과 쪽 번호를 뗀 것이다. http·https 주소만 받는다.
- HTML의 끝 태그 생략(li, tr, td, p 등)을 메우고, 트리는 재귀 없이 훑는다(깊은 페이지에서도 멈추지 않게).
- 받기·안전 검사는 RSS와 같다(sources.rss.fetch_bytes). 자바스크립트로 그리는 페이지와 로그인이 필요한
  페이지는 받지 않는다(1차). 받은 글은 신뢰할 수 없는 입력(A)이다.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from noticedelta.sources.base import NoticeRecord, SourceError

MIN_ITEMS = 3  # 이보다 적게 되풀이되면 글 목록으로 보지 않는다
MAX_ITEMS = 200
MAX_DEPTH = 400  # 이보다 깊은 트리는 끊는다
ROW_TAGS = {"tr", "li", "dd", "dt", "article"}  # 모양에서 class를 빼는 줄 요소
_DATE = re.compile(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")
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
# 끝 태그를 생략해도 되는 요소: 같은 무리가 새로 열리면 앞의 것을 닫는다(경계 안에서만)
_AUTO = {
    "li": ({"li"}, {"ul", "ol"}),
    "tr": ({"tr"}, {"table", "tbody", "thead", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "dd": ({"dd", "dt"}, {"dl"}),
    "dt": ({"dd", "dt"}, {"dl"}),
    "option": ({"option"}, {"select", "datalist"}),
    "p": ({"p"}, {"div", "td", "li", "body"}),
}
_VOLATILE_QUERY = {"jsessionid", "phpsessid", "sid", "sessionid", "page", "pageindex", "pageno",
                   "cpage", "curpage", "pg"}  # fmt: skip
KEYS = {
    "guid": "글을 가리키는 값(세션값·쪽 번호를 뗀 글 주소)",
    "title": "글 제목(목록의 링크 글)",
    "link": "글 주소",
    "date": "목록에 적힌 날짜(YYYY-MM-DD로 맞춘 것, 없으면 빈 값)",
}


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict
    parent: Node | None = None
    children: list = field(default_factory=list)  # Node 또는 글(str)

    def text(self, skip: Node | None = None) -> str:
        """안의 글(재귀 없이). skip이면 그 노드 안의 글은 뺀다."""
        parts, stack = [], [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                parts.append(n)
            elif n is not skip:
                stack.extend(reversed(n.children))
        return " ".join(" ".join(parts).split())

    def shape(self) -> str:
        if self.tag in ROW_TAGS:
            return self.tag
        classes = ".".join(sorted((self.attrs.get("class") or "").split()))
        return f"{self.tag}.{classes}" if classes else self.tag


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root
        self.depth = 0
        self.skip = 0

    def _close_to(self, node: Node) -> None:
        while self.cur is not node and self.cur is not self.root:
            self.cur, self.depth = self.cur.parent, self.depth - 1
        if self.cur is not self.root:
            self.cur, self.depth = self.cur.parent, self.depth - 1

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _AUTO:  # 끝 태그를 생략한 앞 줄을 닫는다
            same, bounds = _AUTO[tag]
            n = self.cur
            while n is not self.root and n.tag not in bounds:
                if n.tag in same:
                    self._close_to(n)
                    break
                n = n.parent
        node = Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID and self.depth < MAX_DEPTH:
            self.cur, self.depth = node, self.depth + 1

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self._close_to(n)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.cur.children.append(data)


def _tree(html: str) -> Node:
    t = _Tree()
    try:
        t.feed(html)
        t.close()
    except Exception as e:  # 깨진 페이지: 수집 실행 전체를 멈추지 않게 출처 오류로
        raise SourceError(f"페이지를 읽지 못했다: {type(e).__name__}") from None
    return t.root


def _anchors(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        n = stack.pop()
        if n.tag == "a" and n.attrs.get("href"):
            yield n
        stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))


def _ancestors(n: Node) -> list[Node]:
    out, p = [], n.parent
    while p is not None and p.tag != "#root":
        out.append(p)
        p = p.parent
    return out


def _path(a: Node) -> str:
    chain = [a, *_ancestors(a)]
    return ">".join(n.shape() for n in reversed(chain) if n.tag not in ("html", "body"))


def _guid(link: str) -> str:
    """세션값과 쪽 번호를 뗀 주소(받을 때마다 바뀌는 값 때문에 같은 글이 새 글이 되지 않게)."""
    u = urlsplit(link)
    path = re.sub(r";jsessionid=[^/?#]*", "", u.path, flags=re.I)
    query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if k.lower() not in _VOLATILE_QUERY]  # fmt: skip
    return urlunsplit((u.scheme, u.netloc.lower(), path, urlencode(query), ""))


def _date(text: str) -> str:
    for m in reversed(list(_DATE.finditer(text))):  # 줄의 뒤쪽(날짜 칸)부터
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            continue  # 있을 수 없는 날짜
    return ""


def _items(root: Node, base: str) -> dict[str, list[tuple[Node, dict]]]:
    """모양별 (링크, 글) 목록. 줄(row)은 같은 모양의 다른 링크가 섞이기 직전까지의 가장 높은 조상."""
    groups: dict[str, list[tuple[Node, dict]]] = defaultdict(list)
    for a in _anchors(root):
        href = a.attrs.get("href", "").strip()
        link = urldefrag(urljoin(base, href))[0]
        if urlsplit(link).scheme not in ("http", "https"):
            continue  # javascript:, data:, mailto: …
        title = a.text()
        if len(title) < 2:
            continue
        groups[_path(a)].append((a, {"guid": _guid(link), "title": title[:500], "link": link}))
    for members in groups.values():
        shared = Counter(id(n) for a, _ in members for n in _ancestors(a))
        for a, item in members:
            row = a
            for n in _ancestors(a):
                if shared[id(n)] > 1:
                    break
                row = n
            item["date"] = _date(row.text(skip=a)) if row is not a else ""
    return groups


def decode(data: bytes) -> str:
    """받은 바이트를 글로. UTF-8이 아니면 한국 공공 사이트에 흔한 CP949(EUC-KR)로 읽는다."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def candidates(html: str, base: str) -> list[dict]:
    """글 목록으로 보이는 곳들(그럴듯한 순). 후보마다 위치(list), 글 수, 앞의 글 몇 개."""
    out = []
    for path, members in _items(_tree(html), base).items():
        unique = list({it["guid"]: it for _, it in members}.values())
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
    """고른 위치(list)의 글(원본 사전). 같은 글(guid)은 한 번만."""
    seen: dict[str, dict] = {}
    for _, item in _items(_tree(html), base).get(list_path, []):
        seen.setdefault(item["guid"], item)
    return list(seen.values())[:MAX_ITEMS]


def to_record(source_id: str) -> Callable[[dict], NoticeRecord]:
    """게시판 글(원본 사전)을 NoticeRecord로. 글 본문은 받지 않는다(1차: 목록의 제목·날짜가 바뀌면 변경)."""
    from noticedelta.sources.rss import to_record as feed_record

    feed = feed_record(source_id)

    def convert(raw: dict) -> NoticeRecord:
        if not raw.get("link"):
            raise SourceError("글 주소가 없다")
        rec = feed({"guid": raw.get("guid") or raw["link"], "title": raw.get("title"),
                    "link": raw["link"], "description": raw.get("date") or ""})  # fmt: skip
        return replace(rec, raw={k: raw.get(k) for k in KEYS})

    return convert
