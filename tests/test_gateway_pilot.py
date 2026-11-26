"""RQ6 파일럿(gateway_pilot): 숫자가 바뀌면 알 수 있게 지금의 결과를 고정한다."""

from noticedelta.gateway_pilot import TASKS, run


def test_task_set_shape():
    assert sum(t.attack for t in TASKS) == 10 and len(TASKS) == 20


def test_gateway_asks_less_and_leaks_nothing():
    rows = {r["policy"]: r for r in run((0.9,))}
    sandbox, per_effect, gateway = rows["sandbox"], rows["per_effect"], rows["gateway"]
    assert sandbox["harm"] == 12 and sandbox["leaks"] == 10  # 넘어간 에이전트는 다 해낸다
    assert per_effect["asked_per_task"] == 0.95 and gateway["asked_per_task"] == 0.3
    assert gateway["harm"] < per_effect["harm"] and gateway["leaks"] == 0
    assert (
        gateway["benign_trimmed"] == 2
    )  # 대가: 사용자가 시킨 자유 글이 빠진다(빈 본문은 세지 않음)
    # 공격이 통한 과제는 1 - Π(1 - p): 세 번 보내는 X8 때문에 효과 하나씩의 최댓값보다 크다
    assert per_effect["harm_tasks"] == 1.17 and gateway["harm_tasks"] == 0.47
