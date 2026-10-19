# 실제 API 응답 확인 (2026-10-19)

> `scripts/capture_samples.py`로 두 출처를 실제 키로 호출해 응답 형태를 확인한 기록.
> 원본 응답은 `data/samples/`(gitignore)에만 두고 저장소에는 올리지 않는다(약관, `terms.md`).
> 테스트 픽스처는 여기서 확인한 형태를 따르고 값은 모두 가상이다.

## 공통 (data.go.kr)

| 항목 | 확인 결과 | 반영 |
|---|---|---|
| 인증키 | 마이페이지에 Encoding판(`%2B` 등)과 Decoding판이 따로 나온다. httpx가 쿼리를 다시 인코딩하므로 Encoding판을 그대로 쓰면 인증에 실패한다 | `normalize_service_key`: `%`가 보이면 한 번 풀어 쓴다 |
| 잘못된 키 | HTTP 403 + JSON `OpenAPI_ServiceResponse.cmmMsgHeader.errMsg = SERVICE_KEY_IS_NOT_REGISTERED_ERROR` (두 출처 같음) | 오류 메시지에 `errMsg`를 붙인다. 키는 넣지 않는다. 200으로 같은 봉투가 와도 오류로 본다 |

## K-Startup 사업공고 (15125364)

| 항목 | 확인 결과 |
|---|---|
| 봉투 | `{currentCount, matchCount, page, perPage, totalCount, data: [...]}`. 명세의 `data.data[]`가 아니라 `data[]` |
| 빈 페이지 | HTTP 200, `data: []` |
| 전체 건수 | `totalCount` 약 3만 건(마감 공고 포함) |
| `pbanc_sn` | 정수 |
| 날짜 | `pbanc_rcpt_bgng_dt`, `pbanc_rcpt_end_dt` 모두 `yyyyMMdd` 문자열 |
| 분류 목록 | `biz_enyy`(업력), `biz_trgt_age`(연령), `aply_trgt`(신청 대상)은 쉼표로 이은 분류 목록. 예: 업력은 "예비창업자,1년미만,…,10년미만" 형태 |
| 비어 있는 필드 | 표본 5건 중 `prfn_matr`(우대 사항), `biz_aply_url`, 접수 방법 대부분(이메일·팩스·방문·우편)이 전부 null |
| 첨부 | 첨부 필드 없음 (예상대로) |
| 새로 쓰는 필드 | `aply_trgt`, `supt_biz_clsfc`(지원 분야), `biz_prch_dprt_nm`(담당 부서), `prch_cnpl_no`(문의처)를 본문 블록에 넣는다 |

**나중에 쓸 것**: `biz_enyy`·`biz_trgt_age`·`supt_regin`은 기관이 고른 구조화된 조건이다. LLM 추출 전에 이 값으로 자격 규칙의 초안을 만들거나, LLM 추출 결과를 교차 확인하는 데 쓸 수 있다(작업 목록 5번).

## 기업마당 지원사업정보 (15157820)

| 항목 | 확인 결과 |
|---|---|
| 봉투 | `response.header.{resultCode: "00", resultMsg: "NORMAL_SERVICE"}`, `response.body.{items: {item: [...]}, numOfRows, pageNo, totalCount}` |
| 한 건일 때 | `item`은 여전히 배열 |
| 빈 페이지 | `items: {}` (item 키 없음) |
| 전체 건수 | `totalCount` 약 1,400건 |
| 날짜 | `reqstBeginEndDe` = `YYYY-MM-DD ~ YYYY-MM-DD`, `creatPnttm`·`updtPnttm` = `YYYY-MM-DD HH:MM:SS` |
| **`updtPnttm`** | 표본 5건 중 4건이 같은 초(15:07:05)에 수정됨 → 일괄 갱신으로 보인다. 내용이 그대로여도 값이 바뀌므로 **내용 해시에서 뺀다** (조회수 `inqireCo`도 뺀다) |
| **공고문 본 파일** | `printFileNm`/`printFlpthNm`에 공고문 하나(HWPX 또는 PDF)가 따로 온다. 5건 모두 있음. `fileNm`/`flpthNm`(붙임 서식, `@`로 이음)은 5건 중 2건이 null |
| 문의처 | `refrncNm`에 기관·전화·메일이 자유 형식으로 |
| 기타 | `pldirSportRealmLclasCodeNm`(지원 분야), `rceptEngnHmpgUrl`(접수 사이트, 대부분 null), `hashtags` |

반영:
- 응답 형태는 위 하나만 받는다. `response`가 없으면 빈 목록이 아니라 **오류**로 낸다(형태가 바뀌었는데 수집이 조용히 멈추지 않게).
- `resultCode`가 `"00"`이 아니면 오류.
- 공고문 본 파일을 `role="notice"` 첨부로 맨 앞에 넣고, 붙임 서식은 `role="attachment"`.
- 기업마당 자체 API(`crtfcKey`, `jsonArray`)는 키가 없어 아직 확인하지 못했다. 그쪽 봉투 처리는 기존대로 둔다.

## 수집 → 저장 실연동 (로컬 Docker)

- PostgreSQL 16(pgvector) + SeaweedFS(S3 호환)에 두 출처 5건씩 기록: 공고 10건, 버전 10건, 첨부 9건(HWPX 5, PDF 4), 실패 0건. 첨부 합계 약 4.8MB.
- 같은 명령을 다시 돌리면 새 버전 0건, 새로 받은 첨부 0건, 저장소 객체 수 그대로(중복 저장 없음).
- 이 과정에서 **버전보다 첨부 행이 먼저 INSERT되는 외래 키 오류**를 찾아 고쳤다. SQLite는 기본으로 외래 키를 검사하지 않아 테스트에서 놓쳤으므로, 테스트 DB도 외래 키 검사를 켠다(`tests/conftest.py`).
- MinIO는 2025년 말 커뮤니티 이미지 배포를 멈춰 Docker Hub·quay.io 모두에서 받을 수 없었다. 로컬 S3 호환 저장소는 SeaweedFS 4.48로 바꿨다.
- 로컬 PC에 PostgreSQL이 따로 설치돼 5432를 쓰고 있어, compose의 PostgreSQL은 5433으로 연다.

재현:

```bash
docker compose up -d
uv run --extra s3 --extra postgres python scripts/ingest_live.py --per-source 5
```

(`.env`에 `DATABASE_URL=postgresql+psycopg://noticedelta:noticedelta@localhost:5433/noticedelta`, `NOTICEDELTA_BLOB_BACKEND=s3`, `S3_ENDPOINT_URL=http://localhost:9000`, `S3_ACCESS_KEY_ID=noticedelta`, `S3_SECRET_ACCESS_KEY=noticedelta-dev`, `S3_REGION=us-east-1`)
