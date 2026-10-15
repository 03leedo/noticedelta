# 데모 녹화본

이 폴더의 녹화본은 실제 LLM 응답이 아니라 `demo/scenario.json`의 `fake_llm_answers`에 손으로 적은 **가상 응답**이다.
`uv run python -m noticedelta.demo --record-fake`로 다시 만든다. 파일 이름은 요청 해시(`noticedelta.llm.request_key`)다.

실제 API로 녹화하려면 `NOTICEDELTA_LLM_MODE=record`와 `ANTHROPIC_API_KEY`를 설정하고 실제 공고로 돌린다(4주차).
