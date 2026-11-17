"""글의 표기 정규화. 비교와 해시에만 쓴다(보여 줄 글이 아니다)."""

from __future__ import annotations

import html


def canonical_text(text: str) -> str:
    """내용과 무관한 표기 차이를 지운 글.

    HTML 문자 표기(&apos; 등)를 풀고 공백·줄바꿈을 한 칸으로 줄인다. 출처가 표기 방식만 바꿔도
    같은 글로 본다(2026-11-17 K-Startup이 &apos;를 '로 바꿔 내보내기 시작했다).
    두 번 감싼 표기(&amp;apos;)까지 푼다.
    """
    for _ in range(3):
        plain = html.unescape(text)
        if plain == text:
            break
        text = plain
    return " ".join(text.split())
