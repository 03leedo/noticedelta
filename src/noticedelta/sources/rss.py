"""RSS·Atom 피드 커넥터 (source-catalog.md 3장의 `rss`): 사용자가 등록하는 첫 출처 종류.

    items = fetch_feed(url, http)            # 안전한 주소인지 보고 받아서 항목(원본 사전) 목록으로
    rec = to_record("my-feed")(items[0])     # 플랫폼이 쓰는 NoticeRecord로

안전(6장):
- http·https만, 표준 포트만, 이름이 사설·루프백·링크 로컬(클라우드 메타데이터)·예약 주소로 풀리면 막는다.
  다른 주소로 넘기면(리다이렉트) 그 주소도 다시 본다.
- 받는 크기를 자르고, DOCTYPE·ENTITY 선언이 있는 XML은 읽지 않는다(엔티티 폭탄·외부 엔티티).
- 받은 글은 신뢰할 수 없는 입력(A)이다. 본문은 태그를 지운 글로만 남긴다.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from noticedelta.sources.base import NoticeRecord, SourceError

MAX_BYTES = 2_000_000
MAX_ITEMS = 200
MAX_REDIRECTS = 3
KEYS = {
    "guid": "글을 가리키는 값(없으면 주소)",
    "title": "글 제목",
    "link": "글 주소",
    "description": "글 요약이나 본문(HTML을 지운 글)",
    "published": "올린 시각(피드가 준 그대로)",
}
_ATOM = "{http://www.w3.org/2005/Atom}"


class UnsafeURL(SourceError):
    """받으면 안 되는 주소."""


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _public(host: str) -> bool:
    """이름이 풀리는 주소가 모두 공개 주소인가."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and not any(_blocked(ipaddress.ip_address(i[4][0])) for i in infos)


def check_url(url: str, *, resolve: Callable[[str], bool] = _public) -> httpx.URL:
    """받아도 되는 주소인지 본다. 안 되면 UnsafeURL. IP를 바로 쓴 주소는 이름 풀이 없이 직접 본다."""
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise UnsafeURL("주소 형식이 잘못됐다") from None
    if u.scheme not in ("http", "https"):
        raise UnsafeURL(f"http·https만 받는다: {u.scheme}")
    if u.port not in (None, 80, 443):
        raise UnsafeURL(f"표준 포트만 받는다: {u.port}")
    if not u.host:
        raise UnsafeURL("호스트가 없다")
    try:
        ip = ipaddress.ip_address(u.host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if _blocked(ip):
            raise UnsafeURL(f"사설·내부 주소는 받지 않는다: {u.host}")
    elif u.host.lower() == "localhost" or u.host.lower().endswith(".localhost"):
        raise UnsafeURL(f"사설·내부 주소는 받지 않는다: {u.host}")
    elif not resolve(u.host):
        raise UnsafeURL(f"사설·내부 주소로 풀리거나 찾을 수 없는 이름: {u.host}")
    return u


def fetch_bytes(url: str, http: httpx.Client, *, resolve: Callable[[str], bool] = _public) -> bytes:
    """주소를 받는다. 넘겨주기마다 주소를 다시 보고, 크기를 자른다."""
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolve=resolve)
        try:
            with http.stream("GET", url, follow_redirects=False, timeout=20) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise SourceError(f"피드를 받지 못했다: HTTP {resp.status_code}")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise SourceError(f"피드가 너무 크다({MAX_BYTES:,}바이트 넘음)")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as e:
            raise SourceError(f"피드를 받지 못했다: {type(e).__name__}") from None
    raise SourceError("넘겨주기가 너무 많다")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    p = _Text()
    p.feed(value)
    return re.sub(r"\s+", " ", html.unescape(" ".join(p.parts))).strip()


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _first(el: ET.Element, *paths: str) -> ET.Element | None:
    """처음 찾은 것(Element의 참거짓은 자식 수라 `or`로 고르면 안 된다)."""
    for path in paths:
        found = el.find(path)
        if found is not None:
            return found
    return None


def parse_feed(data: bytes) -> tuple[str, list[dict]]:
    """피드의 제목과 항목(원본 사전) 목록. RSS 2.0과 Atom을 읽는다."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise SourceError("DOCTYPE·ENTITY가 있는 피드는 읽지 않는다")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SourceError(f"피드가 XML이 아니다: {e}") from None
    items: list[dict] = []
    if root.tag == f"{_ATOM}feed":
        title = _text(root.find(f"{_ATOM}title"))
        for e in root.findall(f"{_ATOM}entry")[:MAX_ITEMS]:
            link = _first(e, f"{_ATOM}link[@rel='alternate']", f"{_ATOM}link")
            href = link.get("href", "") if link is not None else ""
            summary = _first(e, f"{_ATOM}summary", f"{_ATOM}content")
            items.append({
                "guid": _text(e.find(f"{_ATOM}id")) or href,
                "title": _text(e.find(f"{_ATOM}title")),
                "link": href,
                "description": strip_html(_text(summary)),
                "published": _text(e.find(f"{_ATOM}updated")) or _text(e.find(f"{_ATOM}published")),
            })  # fmt: skip
        return title, items
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise SourceError("RSS나 Atom 피드가 아니다")
    title = _text(channel.find("title"))
    for it in channel.findall("item")[:MAX_ITEMS]:
        link = _text(it.find("link"))
        items.append({
            "guid": _text(it.find("guid")) or link,
            "title": _text(it.find("title")),
            "link": link,
            "description": strip_html(_text(it.find("description"))),
            "published": _text(it.find("pubDate")),
        })  # fmt: skip
    return title, items


def fetch_feed(
    url: str, http: httpx.Client, *, resolve: Callable[[str], bool] = _public
) -> tuple[str, list[dict]]:
    return parse_feed(fetch_bytes(url, http, resolve=resolve))


def to_record(source_id: str) -> Callable[[dict], NoticeRecord]:
    """피드 항목(원본 사전)을 NoticeRecord로. 첨부는 받지 않는다(1차)."""

    def convert(raw: dict) -> NoticeRecord:
        guid = str(raw.get("guid") or raw.get("link") or "").strip()
        if not guid:
            raise SourceError("글을 가리키는 값(guid·link)이 없다")
        if len(guid) > 100:  # 게시물 id 칸에 맞춘다(자르면 겹칠 수 있어 해시로)
            guid = "h:" + hashlib.sha256(guid.encode("utf-8")).hexdigest()[:40]
        body = "\n\n".join(x for x in (raw.get("title"), raw.get("description")) if x)
        return NoticeRecord(
            source=source_id,
            external_id=guid,
            title=str(raw.get("title") or "").strip()[:500] or guid[:100],
            org=None,  # 기관이 없으면 항목을 묶지 않는다(게시물 하나가 항목 하나)
            url=str(raw.get("link") or "") or None,
            body=body,
            apply_start=None,
            apply_end=None,
            raw={k: raw.get(k) for k in KEYS},
        )

    return convert
