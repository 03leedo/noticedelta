from noticedelta.demo import run_demo
from noticedelta.llm import Mode


def test_demo_replays_scenario_without_network():
    lines = []
    res = run_demo(mode=Mode.REPLAY, out=lines.append)
    assert sorted((a.user_id, a.kind, a.field) for a in res.alerts) == [
        ("A", "judgment_changed", "eligibility_rules"),
        ("B", "field_changed", "deadline"),
    ]
    assert "contact" in res.affected_fields
    assert "contact" not in res.real_changes  # 문구만 바뀐 문의처
    assert res.impact_llm_calls == 1
    assert res.baseline_llm_calls > res.impact_llm_calls
    assert res.live_calls == 0
    assert any("가상" in line for line in lines)
