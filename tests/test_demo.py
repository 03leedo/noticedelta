import pytest

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


def test_demo_dir_from_env_and_clear_error(tmp_path, monkeypatch):
    from noticedelta import demo

    (tmp_path / "scenario.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NOTICEDELTA_DEMO_DIR", str(tmp_path))
    assert demo.find_demo_dir() == tmp_path

    monkeypatch.setenv("NOTICEDELTA_DEMO_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(demo, "__file__", str(tmp_path / "a" / "b" / "c" / "demo.py"))
    monkeypatch.chdir(tmp_path / "..")
    with pytest.raises(FileNotFoundError, match="scenario.json"):
        demo.find_demo_dir()
