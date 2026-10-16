# NoticeDelta 기반 구현 계획 (1~4주차)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 설계 문서 7장의 핵심 단계(조각화 → 조각 맞추기 → 추출·인용 검증 → 판정 → 변경 영향 엔진)를 네트워크·API 키 없이 테스트로 검증 가능한 Python 패키지로 만들고, 수집기·LLM 게이트웨이·DB 모델·로컬 인프라 뼈대를 붙인다.

**Architecture:** 순수 함수 코어(`segments`, `matching`, `values`, `citation`, `eligibility`, `impact`)를 먼저 만들고, 바깥에 입출력 어댑터(`sources`, `llm`, `db`)를 붙인다. 코어는 DB·네트워크를 모르고, 어댑터는 코어의 데이터 클래스만 주고받는다. 큐·워커(Redis Streams)는 이 계획 범위 밖(5주차 이후).

**Tech Stack:** Python 3.12, uv, pytest, ruff, pydantic v2, httpx, SQLAlchemy 2.0, anthropic SDK. 로컬 인프라는 docker compose(PostgreSQL 16 + pgvector, Redis 7, MinIO).

**Spec:** `docs/DESIGN.md` (v0.2)

## Global Constraints

- Python `>=3.12`, 패키지 경로 `src/noticedelta/`, 테스트 `tests/`.
- 테스트는 네트워크·API 키·Docker 없이 통과해야 한다. 외부 호출은 가짜 객체나 녹화본으로 대체한다.
- 자격 판정 결과는 정확히 세 값: `eligible` / `ineligible` / `unknown` (화면 표기: 해당 / 비해당 / 확인 필요).
- 규칙 연산자: `== != < <= > >= in not_in between`, 그룹: `all_of / any_of`.
- 속성 사전: `is_pre_founder, business_age_months, ceo_age, region_sido, industry_ksic, business_type(individual/corporation), revenue_krw, employees, is_women_owned, is_restart`.
- 날짜는 KST(`Asia/Seoul`)로 정규화, 금액은 원 단위 정수.
- LLM 게이트웨이 모드: `live / record / replay / replay_or_live`. 키 = `sha256(model, prompt_template_version, input, output_schema)`.
- 기본 LLM 모델: `claude-haiku-4-5`.
- 알림 멱등 키: `(user_id, notice_id, field, to_version_id)`.
- 모든 시연·테스트용 공고 데이터는 "가상" 표기. 실제 기관 공고처럼 보이게 만들지 않는다.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **한국어 날짜·금액 표기 다양성**: `2026. 10. 31.(금) 18:00`, `2026-10-31`, `'26.10.31`, `최대 1억 원`, `5,000만원`, `3천만 원`, `1억 5천만원` → 모두 같은 정규값이어야 한다. (Task 3 테스트)
2. **같은 문장이 여러 번 나오는 공고**: 해시가 같은 조각이 둘 이상일 때 순서대로 짝지어야 하고 하나만 이어 붙이면 안 된다. (Task 2 테스트)
3. **프로필에 값이 없는 속성**: 비해당이 아니라 확인 필요. 단, 다른 규칙이 확정적으로 거짓이면 비해당. (Task 4 테스트)
4. **정정 버전이 순서 뒤바뀌어 도착**: 2차 정정 뒤에 1차가 처리되면 현재 버전이 1차로 되돌아가면 안 된다. (Task 8 테스트)
5. **문구만 바뀐 정정**: "10월 31일" → "10. 31." 같은 변경은 알림을 만들지 않는다. (Task 6 테스트)

---

### Task 0: 프로젝트 뼈대와 설계 문서 v0.2

**Files:**
- Create: `pyproject.toml`, `src/noticedelta/__init__.py`, `tests/test_smoke.py`, `.gitignore`, `.env.example`, `Makefile`, `.github/workflows/ci.yml`
- Create: `docs/research/terms.md`, `docs/research/corrections.md`
- Modify: `docs/DESIGN.md` (상태 v0.2, 7.1 수집원 결정, 12.1 정답 출처, 14장 일정, 15장 확인 결과)

- [ ] `uv init --lib` 대신 직접 `pyproject.toml` 작성(hatchling, src 레이아웃, dev 그룹: pytest, ruff).
- [ ] `tests/test_smoke.py::test_version` — `noticedelta.__version__ == "0.1.0"`.
- [ ] `uv run pytest` PASS, `uv run ruff check .` PASS.
- [ ] CI: ubuntu-latest, `astral-sh/setup-uv`, `uv sync`, `ruff check`, `pytest`.
- [ ] DESIGN.md: 구현 시작 10/13, 진행 판단 10/12, K-Startup=본문(API), 기업마당=첨부(API), 스크래핑 안 함, 정정 표본 28건 결과 반영.
- [ ] Commit.

### Task 1: 조각화 (`segments.py`)

**Files:** Create `src/noticedelta/segments.py`, `tests/test_segments.py`

**Interfaces — Produces:**
- `@dataclass(frozen=True) class Segment: kind: Literal["heading","paragraph","table_cell"]; text: str; text_hash: str; locator: dict; order: int; lineage_id: str | None = None`
- `@dataclass(frozen=True) class Table: rows: list[list[str]]; caption: str = ""` (첫 행 = 머리글)
- `def normalize_text(text: str) -> str` — NFC, 공백 압축, 앞뒤 공백 제거
- `def text_hash(text: str) -> str` — `sha256(normalize_text(text))` 16진수
- `def segment_document(blocks: Sequence[str | Table], *, source: str) -> list[Segment]`

- [ ] 테스트: 빈 줄로 나뉜 문단 → paragraph 조각; `□`, `○`, `1.`로 시작하는 짧은 줄 → heading; `Table` → 머리글 제외 칸마다 table_cell, `locator == {"source": src, "table": i, "row": r, "col": c, "row_key": 첫 열 값, "header": 열 머리글}`; 공백만 다른 두 문단은 같은 `text_hash`; `order`는 0부터 연속.
- [ ] 구현 → PASS → Commit.

### Task 2: 조각 맞추기 (`matching.py`)

**Files:** Create `src/noticedelta/matching.py`, `tests/test_matching.py`

**Interfaces — Consumes:** `Segment`. **Produces:**
- `class MatchMode(StrEnum): HASH="hash"; HASH_SIM="hash_sim"`
- `@dataclass class ChangeSet: segments: list[Segment]  # 새 버전, lineage_id 채워짐; added: list[str]; removed: list[str]; modified: list[str]; unchanged: list[str]` (모두 lineage_id)
- `def similarity(a: str, b: str) -> float` — 문자 3-gram Jaccard
- `def match_segments(old: Sequence[Segment], new: Sequence[Segment], *, mode=MatchMode.HASH_SIM, threshold: float = 0.5, new_id: Callable[[], str] = ...) -> ChangeSet`
- `old`가 비어 있으면 모든 조각을 added로, 새 lineage_id 발급.

알고리즘:
1. 표 칸은 `(table header 서명, row_key, header)`로 먼저 짝짓는다. 같은 키면 텍스트가 달라도 modified.
2. 나머지는 `difflib.SequenceMatcher`로 text_hash 시퀀스를 정렬해 equal 블록을 unchanged로 잇는다(같은 해시 여러 개도 순서대로).
3. replace/insert/delete 구간 안에서 HASH_SIM 모드면 유사도 ≥ threshold인 쌍을 탐욕적으로(높은 순) 짝지어 modified. 나머지는 added/removed. HASH 모드는 3단계 없이 바로 added/removed.

- [ ] 테스트: 변경 없음 → 모두 unchanged, lineage 유지; 문단 하나 수정 → modified 1, lineage 유지; 삽입·삭제; **같은 문장 두 번 → 둘 다 각자 lineage 유지**; HASH 모드에서 수정 → removed+added; 표 칸 값 7년→5년 → 해당 칸 modified; 행 순서가 바뀐 표 → unchanged.
- [ ] 구현 → PASS → Commit.

### Task 3: 값 정규화와 타입 비교 (`values.py`)

**Files:** Create `src/noticedelta/values.py`, `tests/test_values.py`

**Produces:**
- `def parse_kst_datetime(text: str, *, default_year: int | None = None) -> datetime | None` (tz=Asia/Seoul, 시각 없으면 23:59)
- `def find_datetimes(text: str, *, default_year: int | None = None) -> list[datetime]`
- `def parse_krw(text: str) -> int | None`, `def find_amounts(text: str) -> list[int]`
- `def values_equal(value_type: Literal["datetime","amount","rules","text","list"], a: Any, b: Any) -> bool`

- [ ] 테스트(Review Focus 1의 값 전부): `parse_krw("1억 5천만원") == 150_000_000`, `parse_krw("5,000만원") == 50_000_000`, `parse_kst_datetime("2026. 10. 31.(금) 18:00")` == 2026-10-31 18:00 KST, `"'26.10.31"` 동일 날짜 23:59; `values_equal("rules", ...)`은 규칙 순서 무관; `values_equal("list", ...)`은 공백·순서 무관.
- [ ] 구현 → PASS → Commit.

### Task 4: 자격 규칙과 판정기 (`eligibility/`)

**Files:** Create `src/noticedelta/eligibility/{__init__,rules,judge,sql}.py`, `tests/test_eligibility.py`

**Produces:**
- pydantic: `Rule(id: str, attribute: str, op: Op, value: Any, source: str | None, confidence: float = 1.0)`, `ManualCondition(text: str, source: str | None)`, `RuleGroup(group: Literal["all_of","any_of"], rules: list[Rule | RuleGroup], manual: list[ManualCondition] = [])`
- `ATTRIBUTES: dict[str, type]` (Global Constraints의 사전), 사전 밖 속성은 검증 오류.
- `@dataclass class Reason: rule_id: str | None; outcome: Literal["pass","fail","missing","low_confidence","manual"]; detail: str; source: str | None`
- `@dataclass class Judgment: result: Literal["eligible","ineligible","unknown"]; reasons: list[Reason]`
- `def judge(group: RuleGroup, profile: Mapping[str, Any], *, min_confidence: float = 0.7) -> Judgment`
- `def to_sql_where(group: RuleGroup, column: Callable[[str], ColumnElement]) -> ColumnElement` (대량 판정용. 값 없는 속성은 NULL → 확인 필요 쪽으로 남도록 "확정 비해당" 조건만 생성)

3치 논리: all_of = 하나라도 fail → fail, 아니면 unknown 있으면 unknown, 아니면 pass. any_of = 하나라도 pass → pass, 아니면 unknown 있으면 unknown, 아니면 fail. manual이 있으면 pass를 unknown으로 낮춘다.

- [ ] 테스트: 업력 72개월 + `<= 84` → eligible; 업력 72 + `<= 60` → ineligible(사유에 source 포함); 업력 없음 → unknown; **업력 없음 + 지역 fail → ineligible**; confidence 0.5 규칙 → unknown; manual 있으면 eligible→unknown; any_of; between; not_in; 사전 밖 속성 → ValidationError; `to_sql_where`를 SQLite 메모리 DB에 적용해 확정 비해당 사용자만 걸러지는지.
- [ ] 구현 → PASS → Commit.

### Task 5: 인용 검증 (`citation.py`)

**Files:** Create `src/noticedelta/citation.py`, `tests/test_citation.py`

**Consumes:** `values.find_datetimes`, `values.find_amounts`. **Produces:**
- `def verify_citation(value_type: str, value: Any, cited_texts: Sequence[str], *, default_year: int | None = None) -> bool` — datetime·amount는 인용 텍스트에서 같은 정규값이 나와야 True, 숫자가 든 규칙은 그 숫자가 텍스트에 있어야 True, text는 정규화 부분 문자열.

- [ ] 테스트: 마감 10/31이 인용 조각에 있으면 True, 없으면 False; 금액 `1억원` vs 텍스트 `100,000,000원` True; 규칙 `<= 84`(개월)인데 텍스트가 `7년 이내` → True(년→개월 환산 허용).
- [ ] 구현 → PASS → Commit.

### Task 6: 변경 영향 엔진 (`impact.py`)

**Files:** Create `src/noticedelta/impact.py`, `tests/test_impact.py`

**Consumes:** `ChangeSet`, `values_equal`, `judge`, `Judgment`. **Produces:**
- `@dataclass class FieldValue: name: str; value_type: str; value: Any; sources: list[str]  # lineage ids; confidence: float = 1.0`
- `@dataclass class Subscription: user_id: str; fields: set[str]`
- `@dataclass class Alert: user_id: str; notice_id: str; field: str; to_version_id: str; kind: Literal["judgment_changed","field_changed"]; before: Any; after: Any; sources: list[str]; idempotency_key: str`
- `class Extractor(Protocol): def __call__(self, fields: set[str], segments: Sequence[Segment]) -> dict[str, FieldValue]`
- `def build_field_source_index(fields: Mapping[str, FieldValue]) -> dict[str, set[str]]` (lineage → 필드)
- `@dataclass class ImpactResult: affected_fields: set[str]; reextracted: dict[str, FieldValue]; real_changes: set[str]; alerts: list[Alert]; llm_fields_requested: int`
- `def run_impact(*, notice_id: str, to_version_id: str, change: ChangeSet, old_fields: Mapping[str, FieldValue], extractor: Extractor, subscriptions: Sequence[Subscription], profiles: Mapping[str, Mapping[str, Any]], old_judgments: Mapping[str, Judgment], candidate_detector: Callable[[Segment], set[str]] = keyword_candidates) -> ImpactResult`
- `def keyword_candidates(seg: Segment) -> set[str]` — 날짜→deadline, 금액→amount, 업력·나이·지역·예비창업 등→eligibility_rules, 서류·제출→required_documents.

흐름은 설계 7.7 ①~⑥ 그대로. 영향 필드 = 바뀐·삭제된 lineage의 색인 필드 ∪ 추가 조각의 후보 필드. 추출기는 영향 필드만, 새 버전 중 관련 조각(바뀐 조각 + 그 필드의 기존 출처 조각)만 받는다. eligibility_rules가 진짜 바뀌면 `old_judgments`에 있는 모든 사용자를 재판정하고 결과가 달라진 사람만 `judgment_changed`. 다른 필드는 그 필드를 구독한 사용자에게 `field_changed`.

- [ ] 테스트(설계 4.2 시나리오): 업력 규칙 84→60개월 정정, 사용자 A(72개월)·B(36개월)·C(값 없음) → A만 judgment_changed, B 알림 없음, C unknown 유지라 없음; **마감일 문구만 바뀜("10월 31일"→"10. 31.") → real_changes 비어 있고 알림 0**; 마감 연장 → deadline 구독자만 field_changed; 변경 없는 ChangeSet → 추출기 호출 0회; 추출기가 받은 필드 집합이 영향 필드와 같음; idempotency_key 형식 `f"{user}:{notice}:{field}:{to_version}"`.
- [ ] 구현 → PASS → Commit.

### Task 7: LLM 게이트웨이와 추출기 (`llm/`)

**Files:** Create `src/noticedelta/llm/{__init__,gateway,cassette,anthropic_provider,extract}.py`, `tests/test_gateway.py`, `tests/test_extract.py`

**Produces:**
- `class Mode(StrEnum): LIVE, RECORD, REPLAY, REPLAY_OR_LIVE`
- `@dataclass class LLMRequest: model: str; template_version: str; system: str; input: str; output_schema: dict`
- `@dataclass class LLMResponse: output: dict; input_tokens: int; output_tokens: int; cost_usd: float; from_cassette: bool`
- `class Provider(Protocol): def complete(self, req: LLMRequest) -> LLMResponse`
- `def request_key(req: LLMRequest) -> str` (sha256 of canonical JSON of model, template_version, system+input, output_schema)
- `class CassetteStore(Protocol)` + `FileCassetteStore(root: Path)` (`<root>/<key[:2]>/<key>.json`)
- `class MissingCassette(Exception)`
- `class Gateway(provider: Provider | None, store: CassetteStore, mode: Mode)` with `.complete(req) -> LLMResponse` and `.usage -> UsageTotals(calls, live_calls, input_tokens, output_tokens, cost_usd)`
- `AnthropicProvider(client, prices)` — 구조화 출력은 도구 하나(`record_fields`)를 강제 호출해 받는다.
- `class LLMExtractor` (impact.Extractor 구현): 조각을 `[lineage_id] text` 줄로 넘기고, 응답의 각 필드 `{value, value_type, sources}`를 `FieldValue`로 바꾼 뒤 `verify_citation` 실패 시 `confidence=0.3`.

- [ ] 테스트: REPLAY에서 녹화본 없으면 MissingCassette; RECORD → 저장 후 REPLAY에서 provider 호출 없이 같은 결과; 같은 요청 키 안정성(dict 순서 무관); REPLAY_OR_LIVE; usage 집계; LLMExtractor가 인용 검증 실패 값을 낮은 신뢰도로 표시.
- [ ] 구현 → PASS → Commit.

### Task 8: 수집기와 DB 모델 (`sources/`, `db/`)

**Files:** Create `src/noticedelta/sources/{__init__,base,kstartup,bizinfo,resilience}.py`, `src/noticedelta/db/{__init__,models,repo}.py`, `tests/fixtures/*.json`, `tests/test_sources.py`, `tests/test_db.py`

**Produces:**
- `@dataclass class AttachmentRef: filename: str; url: str`
- `@dataclass class NoticeRecord: source: str; external_id: str; title: str; org: str | None; url: str | None; body: str; apply_start: str | None; apply_end: str | None; attachments: list[AttachmentRef]; raw: dict` + `content_hash` 프로퍼티(raw 정렬 JSON의 sha256)
- `KStartupClient(service_key, http: httpx.Client)`.`list_notices(page, per_page) -> list[NoticeRecord]`, `BizinfoClient(crtfc_key, http)`.`list_notices(page, per_page)`. 필드 매핑은 공식 문서에서 확인한 이름만 쓰고 출처 URL을 주석으로 남긴다.
- `RateLimiter(rate_per_sec, clock)`, `CircuitBreaker(failure_threshold=5, reset_after_s=60, clock)`
- SQLAlchemy 모델: 설계 6장 테이블 전부(임베딩 컬럼은 Postgres에서만 pgvector, 테스트는 생략 가능).
- `repo.upsert_notice_version(session, record) -> tuple[Notice, NoticeVersion | None]` (내용 해시 같으면 None), `repo.advance_current_version(session, notice_id, version_id, version_no) -> bool` (조건부 갱신), `repo.enqueue_alert(session, alert) -> bool` (멱등 키 충돌 시 False), `repo.claim_job(session, stage, job_key) -> bool`.

- [ ] 테스트(httpx.MockTransport + fixtures, SQLite 메모리): 응답 → NoticeRecord 매핑; 키가 로그·예외 메시지에 안 나옴; 5회 실패 → 서킷 open, 시간 경과 후 half-open; 같은 내용 재수집 → 새 버전 없음; **2차 버전 반영 뒤 1차 advance → False, 현재 버전 유지**; 같은 알림 두 번 enqueue → 두 번째 False; claim_job 두 번 → 두 번째 False.
- [ ] 구현 → PASS → Commit.

### Task 9: 로컬 인프라와 가상 시나리오 데모

**Files:** Create `docker-compose.yml`, `src/noticedelta/demo.py`, `demo/cassettes/**`, `demo/scenario.json`, `tests/test_demo.py`; Modify `README.md`, `Makefile`

- [ ] docker compose: `postgres`(pgvector/pgvector:pg16), `redis:7`, `minio`, 볼륨·헬스체크. CI에서 `docker compose config -q`.
- [ ] `python -m noticedelta.demo`: 가상 공고 원본 → 정정본(업력 7년→5년, 마감 연장, 문구만 바뀐 문단 하나)을 REPLAY 모드 게이트웨이로 돌려 단계별 결과(조각 변화, 영향 필드, 재추출 필드 수, 진짜 변경, 알림)를 출력. 녹화본은 손으로 만든 가상 응답이며 파일 머리에 그렇게 적는다.
- [ ] 테스트: 데모가 예외 없이 끝나고 알림이 정확히 기대한 사용자에게만 감.
- [ ] README: 빠른 시작(`uv sync`, `make test`, `make demo`), 현재 구현 범위 표.
- [ ] Commit, push, PR.
