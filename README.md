# NoticeDelta (가칭)

정정공고가 나오면 "내 자격·마감·금액이 어떻게 바뀌었는지"만 골라 알려 주는 정부 지원사업 비서.

- 예비·초기 창업자가 프로필을 등록하면 맞는 지원사업을 찾아 **자격을 코드로 판정**하고, 판정마다 원문 근거를 붙인다.
- 공고나 첨부 HWP가 바뀌면 **바뀐 문단에 걸린 필드만 다시 추출**하고, 판정이 실제로 달라진 사용자에게만 알린다.
- LLM 호출이 사용자 수와 무관하도록 설계한 **변경 영향 엔진**, 0까지 줄어드는 자동 확장(k3s + KEDA), LLM 응답 녹화·재생으로 재현 가능한 실험을 함께 만든다.

## 빠른 시작

[uv](https://docs.astral.sh/uv/)와 Python 3.12가 필요하다.

```bash
uv sync
make test      # 또는 uv run pytest
make demo      # 또는 uv run python -m noticedelta.demo
```

데모는 가상 정정공고 시나리오(설계 4.2)를 녹화된 응답으로 재생한다. API 키·네트워크·Docker 없이 돈다.

```text
[정정공고] 바뀐 조각 4개, 추가 0개, 삭제 0개
[영향 분석] 영향 필드: contact, deadline, eligibility_rules
  진짜 바뀐 필드: deadline, eligibility_rules
  contact: 문구만 바뀜, 알림 없음
  LLM 호출: 1회 (질문별 재실행 방식이라면 15회)
[알림]
  사용자 A: 자격 해당 → 비해당 (근거 L7, L9)
  사용자 B: deadline 2026-10-31 18:00 → 2026-11-07 18:00
```

로컬 인프라(PostgreSQL + pgvector, Redis, S3 호환 저장소 SeaweedFS)는 `make up`(`docker compose up -d`)으로 띄운다.
실제 API를 쓰려면 `.env.example`을 `.env`로 복사해 키를 넣는다. `.env`는 저장소에 올리지 않는다.

## 구현 현황 (2026-10-16)

| 설계 | 모듈 | 상태 |
|---|---|---|
| 7.1 수집 | `sources/` K-Startup·기업마당 클라이언트, 속도 제한, 서킷 브레이커 | 실제 키로 응답 형태 확인 ([기록](docs/research/api-responses.md)). 테스트는 그 형태의 가상 응답 |
| 7.2 저장 | `blobstore.py` 내용 해시 키 저장소(로컬·S3 호환), `ingest.py` 새 버전의 첨부 내려받기·형식 판별 | 완료. 로컬 PostgreSQL + SeaweedFS에 실제 공고 10건·첨부 9건 기록 확인 |
| 7.3 격리 파싱·조각화 | `infra/parser` 격리 파서 컨테이너(kordoc, 실패 시 hwp-hwpx-parser), `parsing.py` 파서 출력 계약·검토 기준, `segments.py` 문단·표 칸 조각(쪽·병합 칸·행 키) | 완료. 실제 첨부로 확인 ([파서 비교](docs/research/parsers.md)) |
| 7.4 조각 맞추기 | `matching.py` 해시·순서 정렬·유사도·표 키, `hash` / `hash_sim` 모드 | 완료 (LLM 보조 변형은 RQ2 실험 때) |
| 7.5 추출 | `llm/extract.py` 구조화 출력 추출, `citation.py` 인용 검증 | 완료 |
| 7.6 판정 | `eligibility/` 규칙 형식, 3단계 판정기, 대량 판정용 SQL 조건, 시간에 따라 바뀌는 자격(자격 상실·획득 예정일)과 거의 해당 | 완료. 화면 표시는 5주차 |
| 7.7 변경 영향 엔진 | `impact.py` 영향 필드 → 부분 재추출 → 진짜 변경 → 재판정 → 알림 | 메모리 버전 완료. 큐·워커 연결은 5~6주차 |
| 8 신뢰성 | `db/repo.py` 버전 순서 보장, 아웃박스 멱등, 작업 원장 | 완료 (SQLite 테스트) |
| 10 녹화·재생 | `llm/gateway.py` live / record / replay / replay_or_live, 비용 집계 | 완료 |
| 9 인프라 | `docker-compose.yml` | 데이터 저장소만. k3s·KEDA·Terraform은 9~10주차 |

## 문서

- [설계 문서](docs/DESIGN.md): 목표, 연구 질문, 시스템 구조, 데이터 모델, 평가 계획, 일정. 이 저장소의 기준 문서다.
- [플랫폼 구조](docs/ARCHITECTURE.md): 변경 영향 엔진을 공용 플랫폼으로 넓히고 그 위에 서비스 여러 개를 올리는 큰 틀, 도메인 팩 규약, 이벤트·MCP 계약, 위협 모델, 단계.
- [수집원 약관·robots 확인](docs/research/terms.md), [정정공고 표본 28건](docs/research/corrections.md), [실제 API 응답 확인](docs/research/api-responses.md), [파서 품질 비교](docs/research/parsers.md)
- [구현 계획 (1~4주차)](docs/superpowers/plans/2026-10-16-foundation.md)
- [진행 발표 슬라이드](https://03leedo.github.io/noticedelta/slides/): 9월 진행상황 및 10월 계획 ([원본](docs/slides/index.html))

## 고지

판정과 알림은 참고용이다. 최종 확인은 공고 원문으로 한다. 공고 데이터는 공식 API로만 받고, 출처와 공공누리 유형을 표시한다.
