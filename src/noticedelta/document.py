"""파서가 내는 문서의 모양: 문단·제목(TextBlock)과 표(Table).

문서 파싱(공통)이 만들고 조각화(엔진)가 받는다. 어느 쪽도 상대를 가져다 쓰지 않게 여기 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    """첫 행이 머리글인 표.

    병합 칸이 덮는 자리는 None이다. None 자리는 조각을 만들지 않고, 행 키·열 머리글을 정할 때만
    위(행 병합)나 왼쪽(열 병합) 칸의 값으로 채워 본다.
    """

    rows: list[list[str | None]]
    caption: str = ""
    page: int | None = None


@dataclass(frozen=True)
class TextBlock:
    """파서가 준 문단·제목 하나. 파서가 제목이라고 알려 주면 그대로 제목 조각이 된다."""

    text: str
    heading: bool = False
    page: int | None = None
