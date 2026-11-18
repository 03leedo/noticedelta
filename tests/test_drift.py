"""출처 형식 변화 감시의 판단 (drift.py)."""

from noticedelta.drift import canonical, detect, hold, shifts


def items(n, **values):
    return {str(i): dict(values) for i in range(n)}


def test_shifts_count_kinds_and_ignore_notation():
    before = items(30, a="값", b=None, c="&apos;같음&apos;")
    now = items(30, a=None, b="새 값", c="'같음'")
    s = shifts(before, now)
    assert s["a"].kind == "emptied" and s["b"].kind == "filled" and "c" not in s
    assert (s["a"].changed, s["a"].compared, len(s["a"].samples)) == (30, 30, 3)


def test_detect_needs_many_items_and_a_large_share():
    before = items(30, a="값")
    assert set(detect(before, items(30, a="다른 값"))) == {"a"}
    few = {**before, **{str(i): {"a": "다른 값"} for i in range(5)}}
    assert detect(before, few) == {}  # 5건은 실제 변경일 수 있다
    some = {**before, **{str(i): {"a": "다른 값"} for i in range(12)}}
    assert detect(before, some) == {}  # 12/30은 절반이 안 된다
    assert detect(before, items(30, a="다른 값"), skip={"a"}) == {}


def test_items_only_on_one_side_are_not_compared():
    assert shifts(items(30, a="값"), {"new": {"a": "x"}}) == {}


def test_hold_restores_last_seen_values():
    raw = {"a": None, "b": "새", "keep": "그대로"}
    assert hold(raw, {"a": "옛", "keep": "그대로"}, ["a", "b", "keep"]) == ["a", "b"]
    assert raw == {"a": "옛", "keep": "그대로"}
    assert hold({"a": 1}, None, ["a"]) == []


def test_canonical():
    assert canonical("  ") is None and canonical([]) is None and canonical(0) == 0
    assert canonical(["b", "a"]) == '["b", "a"]' and canonical({"x": 1}) == '{"x": 1}'
