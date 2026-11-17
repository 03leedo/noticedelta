# 앱 정의 규격 (초안 v0)

| 항목 | 내용 |
|---|---|
| 상태 | **초안.** 확정은 두 번째 앱을 실제로 올릴 때 한다(ARCHITECTURE 11장: 첫 앱 하나만 보고 규약을 굳히지 않는다). |
| 작성일 | 2026-11-17 |
| 관계 | ARCHITECTURE 3.4(앱은 서버가 아니라 설정), 3.6(빌더와 검증), 4장(도메인 팩 규약)을 구체화한다. |

---

## 0. 한 줄

**앱은 플랫폼이 돌리는 설정 한 장이다.** 코드를 배포하지 않는다. 어떤 출처에서 무엇을 읽고, 어떤 값을 뽑고, 누구에게 무엇이 바뀌면 알릴지를 적는다. 수집, 파싱, 변경 감지, 판정, 알림, 하네스, 운영은 플랫폼이 한다.

## 1. 왜 지금 필요한가

| 이유 | 지금의 문제 |
|---|---|
| 빌더(조종 도구)가 쓸 대상 | 지원사업 앱은 코드 여러 곳에 흩어져 있다. 에이전트가 앱을 만들려면 만들 "물건"이 있어야 한다 |
| 미리 돌려 보기와 비교의 단위 | "이 설정이었다면", "설정을 바꾸면"을 계산하려면 설정이 데이터여야 하고 판(버전)이 있어야 한다 |
| 엔진에 남은 지원사업 지식이 갈 곳 | 속성 사전, 낱말 목록, 변동 값 이름(ARCHITECTURE 9장)이 엔진·공통 안에 있다 |
| 빈 버전 | K-Startup에서 글은 그대로인데 버전만 생긴 것이 200개다. 앱이 읽지 않는 값만 바뀐 경우다. 앱이 무엇을 읽는지 선언해야 거를 수 있다 |

## 2. 무엇이 설정이고 무엇이 코드인가

**원칙: 네트워크를 쓰거나 임의의 계산을 하는 것은 플랫폼 코드, 그 밖은 앱 설정.** 설정은 검증할 수 있고, 판끼리 비교할 수 있고, 과거 기록에 다시 돌려 볼 수 있고, 안전하다.

| 층 | 누가 쓰나 | 모양 | 들어 있는 것 |
|---|---|---|---|
| **출처 카탈로그** | 플랫폼 운영자 | 코드(어댑터) + 출처 설명 | API 호출·페이지 넘기기·인증, 원본 키 목록, 구조화 메타(접수 기간 등), 변동 값(호출마다 바뀌는 값), 이용 조건, 같은 항목 묶기 규칙. **이미 계속 모으고 있어 과거 기록이 있다** |
| **앱** | 만드는 사람(빌더 에이전트) | 설정 한 장 | 읽을 출처와 키, 뽑을 필드, 구독자 모델, 알림 정책, 민감도, 표시 이름 |
| **플랫폼 함수 목록** | 플랫폼 개발자 | 코드, 이름으로 고른다 | 값 타입과 비교(날짜·금액·규칙·목록), 파생 속성 함수(업력, 나이), 묶기 규칙, 매칭 방식 |

앱이 고를 수 없는 것을 하려면(새 계산이 필요하면) 사용자 엔진(ARCHITECTURE 3.5의 템플릿, 격리 실행)으로 넘어간다. 이 문서의 범위 밖이다.

## 3. 앱 정의 한 장: 지금의 지원사업 앱

저장은 JSON이고(에이전트가 JSON 스키마로 쓰고 검증한다), 사람이 읽는 문서에는 같은 내용을 YAML로 적는다.

```yaml
kind: app
id: grant-alerts
version: 3                  # 고칠 때마다 올린다. 켠 판은 바꾸지 않는다
title: 지원사업 정정 알림
sensitivity: personal       # public | personal | sensitive → 하네스 기본값(3.2)

sources:                    # 카탈로그에서 고른다
  - id: bizinfo
    read: [pblancNm, bsnsSumryCn, trgetNm, reqstBeginEndDe, reqstMthPapersCn, refrncNm]
    attachments: true
  - id: kstartup
    read: [biz_pbanc_nm, pbanc_ctnt, aply_trgt_ctnt, aply_excl_trgt_ctnt, biz_enyy,
           biz_trgt_age, supt_regin, prfn_matr, aply_trgt, supt_biz_clsfc, prch_cnpl_no,
           pbanc_rcpt_bgng_dt, pbanc_rcpt_end_dt]
items:
  group: repost_title       # 카탈로그의 묶기 규칙: 기관이 같고 정정 표시를 뺀 제목이 같으면 한 항목

fields:
  deadline:
    type: datetime
    from: {meta: apply_end}         # 출처가 구조화해 준 값. LLM을 부르지 않는다
    else: extract                   # 메타가 없으면 글에서 뽑는다
    guide: 신청 마감 일시
    keywords: [마감, 접수 기간, 신청 기간, 모집 기간, 소진 시까지]
  amount:
    type: amount
    from: extract
    guide: 기업당 최대 지원 금액(원)
    keywords: [지원금, 지원 규모, 지원 한도, 사업비, "최대 \\d"]
  eligibility_rules:
    type: ruleset                   # 아래 subjects.attributes 위의 규칙식
    from: extract
    guide: 신청 자격
    keywords: [신청 자격, 지원 대상, 자격 요건, 제외 대상, 업력, 연령, 소재, 예비 창업, 매출]
  required_documents: {type: list, from: extract, guide: 제출 서류 이름, keywords: [제출 서류, 구비 서류, 증빙]}
  contact: {type: text, from: extract, guide: 문의처, keywords: [문의, 연락처, 담당자]}

subjects:
  model: rules                      # rules | watchlist | everyone
  rules_field: eligibility_rules
  attributes:
    founded_on:          {type: date, label: 창업일}
    ceo_birth_date:      {type: date, label: 대표자 생년월일}
    business_age_months: {type: int, derive: months_since(founded_on), label: 업력(개월)}
    ceo_age:             {type: int, derive: age(ceo_birth_date), label: 대표자 나이}
    region_sido:         {type: enum, values: [서울, 부산, 대구, …, 제주], label: 사업장 소재지}
    business_type:       {type: enum, values: [individual, corporation], label: 사업자 형태}
    revenue_krw:         {type: int, label: 작년 매출(원)}
    employees:           {type: int, label: 상시 근로자 수}
    is_pre_founder:      {type: bool, label: 예비 창업}
    is_women_owned:      {type: bool, label: 여성기업}
    is_restart:          {type: bool, label: 재창업}

alerts:
  - on: verdict_changed             # 판정이 바뀐 사람에게
    urgent: [eligible→ineligible]   # 나머지는 묶어서
  - on: field_changed
    fields: [deadline, amount, required_documents]
    to: subscribers                 # 이 항목을 구독한 사람에게
  hold:
    mass_impact: 20                 # 한 변경이 이만큼 넘는 사람을 비해당으로 바꾸면 붙잡는다
    min_confidence: 0.7             # 인용 검증을 통과하지 못한 값은 "확인 필요"

labels:
  fields: {deadline: 마감일, amount: 지원 금액, eligibility_rules: 신청 자격,
           required_documents: 제출 서류, contact: 문의처}
  results: {eligible: 해당, ineligible: 비해당, unknown: 확인 필요}
```

## 4. 두 번째 예시로 틀 검증: 약국 앱 (가상)

규격이 지원사업에만 맞는지 보려고 모양이 다른 앱을 하나 더 적어 본다. **출처와 키 이름은 가정이다**(카탈로그에 아직 없다).

```yaml
kind: app
id: pharmacy-permits
version: 1
title: 우리 약국 약 허가 사항 변경 알림
sensitivity: public                 # 재고 목록은 민감 정보가 아니라고 본다

sources:
  - id: mfds_permits                # (가상) 의약품 허가 사항 변경 공개 목록
    read: [item_name, ingredient, change_type, change_text, changed_on]
    attachments: false
items:
  group: by_key                     # 출처가 주는 품목 번호로 묶는다

fields:
  item_name:  {type: text, from: {meta: item_name}}
  ingredient: {type: text, from: {meta: ingredient}}
  dosage:     {type: text, from: extract, guide: 용법·용량, keywords: [용법, 용량, 1회, 1일]}
  warnings:   {type: list, from: extract, guide: 사용상 주의사항 항목, keywords: [주의, 금기, 투여하지]}

subjects:
  model: watchlist                  # 구독자가 낸 목록과 항목 필드가 겹치면 해당
  match: {field: ingredient, normalize: korean_drug_name}
  list_label: 우리 약국 재고 성분

alerts:
  - on: field_changed
    fields: [dosage, warnings]
    to: matched                     # 목록이 겹치는 구독자에게
    urgent: [warnings]
  hold: {mass_impact: 200, min_confidence: 0.7}

labels:
  fields: {dosage: 용법·용량, warnings: 주의사항}
```

**틀이 버틴 것**
- 출처·읽을 키·필드·알림·하네스가 같은 자리에 들어간다.
- 구독자 모델만 바꾸면 된다(`rules` → `watchlist`). 필드의 `from: meta`로 LLM 없이 도는 앱도 된다.

**틀이 드러낸 빈 곳 (열린 질문, 7장)**
- `watchlist`의 맞추기(성분 이름 정규화)는 도메인 지식인데 설정으로 적기 어렵다. 지금은 플랫폼 함수 목록에서 고르게 했다(`korean_drug_name`). 함수 목록이 도메인마다 늘어날 수 있다.
- 출처가 "변경 내역"을 직접 주는 경우(`change_type`, `changed_on`)에는 조각 비교가 필요 없을 수 있다. 엔진을 거치지 않는 경로가 필요한지 정해야 한다.

## 5. 판(버전)과 공유

- **판**: `version`과 내용 해시를 함께 쓴다. 켠 판은 바꾸지 않는다. 플랫폼이 만드는 결과(필드 값, 판정, 알림)에는 어느 앱의 어느 판으로 계산했는지 남긴다. 새 판을 켜면 지금 상태를 새 판으로 다시 계산한다(비용을 먼저 보여 준다). 과거는 다시 쓰지 않는다.
- **수집·원문은 출처 단위로 한 번.** 여러 앱이 같은 출처를 읽어도 한 번 받고 한 번 파싱한다(RQ10).
- **변경은 앱이 읽는 것 기준.** 출처 단위의 버전 하나를 두고(앱들이 읽는 키의 합집합), 본문 조각마다 어느 키에서 왔는지 적어 둔다(`locator.key`). 앱은 자기가 읽는 키의 조각이 바뀐 변경만 받는다. 빈 버전 200개는 이것으로 풀린다.
- **필드 추출은 앱 단위.** 같은 출처를 읽어도 뽑는 필드가 다르면 따로 뽑는다. 같은 정의(필드 이름·타입·안내가 같음)는 녹화본 키가 같아 한 번만 부른다.

## 6. 미리 돌려 보기와 비교

### 6.1 미리 돌려 보기 (backtest)

```text
입력  앱 정의(초안이어도 됨) · 기간(예: 지난 90일) · 구독자(만든 사람의 프로필, 또는 가상 구독자 N명)
돌림  기록된 수집 실행을 시간 순으로 다시 흘린다. 다시 수집하지 않는다.
      앱이 읽는 키로 판을 만들고 → 필드를 뽑고(녹화본, 없으면 비용을 보여 주고 묻는다) → 판정·알림을 계산한다.
      알림은 보내지 않고 목록으로만 낸다.
출력  알림 목록(언제·누구에게·무엇이 어떻게·근거 조각) · 요약(건수, 확인 필요 비율, 붙잡힘, LLM 호출·비용)
```

- 사람은 알림 카드마다 맞다/틀리다를 누른다. 이 표시를 **앱의 시험**으로 저장한다(`expected`). 사람 라벨(5a)과 같은 모양이다.
- LLM 키가 없으면 `from: meta` 필드만 계산된다. 나머지 필드는 "녹화본 없음"으로 표시한다(평가 틀과 같다).

### 6.2 비교 (compare)

```text
입력  두 판(켠 판과 초안, 또는 두 초안) · 같은 기간 · 같은 구독자
출력  생긴 알림 · 사라진 알림 · 내용이 바뀐 알림, 그리고 깨진 시험(맞다고 표시한 알림이 사라지거나 바뀜)
```

- "용량 변경은 빼 줘" → 초안을 만들고 비교 → "알림 3건이 사라집니다. 그중 1건은 전에 맞다고 표시하셨습니다."
- 평가 틀(`evaluation`)의 방식 비교와 같은 엔진을 쓴다. 방식 대신 앱 판을 바꿔 끼우는 것이다.

## 7. 검증 규칙 (앱을 저장하기 전에)

- 출처는 카탈로그에 있어야 하고, `read`의 키는 그 출처가 내는 키여야 한다.
- 필드: 이름이 겹치지 않는다. `type`은 정해진 것(datetime, amount, ruleset, list, text, enum)만. `from: meta`면 카탈로그가 내는 메타 키여야 하고, `extract`면 `guide`가 있어야 한다.
- 구독자: `rules`면 `rules_field`가 ruleset 필드여야 하고 `attributes`가 있어야 한다. `derive`와 `normalize`는 플랫폼 함수 목록에 있는 이름만.
- 알림: `fields`는 정의된 필드여야 한다. `hold`는 범위 안의 값만(예: `mass_impact` 1 이상).
- 민감도: `personal` 이상이면 개인정보 가리기를 끌 수 없다. `sensitive`는 외부 모델로 보내지 않는다(로컬 모델이 생기기 전까지는 `sensitive` 앱을 켤 수 없다).
- 한도: 필드 수, 낱말 수, 구독자 목록 크기, 출처 수.

## 8. 지금 코드에서 옮길 것

| 지금 있는 곳 | 갈 곳 |
|---|---|
| `eligibility.rules.ATTRIBUTES`, `SIDO` | 앱 `subjects.attributes` |
| `eligibility.temporal`의 업력·나이 계산 | 플랫폼 파생 함수(`months_since`, `age`), 앱은 `derive`로 이름만 고른다 |
| `impact._KEYWORDS`, `RULES_FIELD` | 앱 `fields[].keywords`, `subjects.rules_field` |
| `llm.extract`의 `FIELD_TYPES`, `FIELD_GUIDE` | 앱 `fields[].type`, `guide`. 프롬프트 틀은 플랫폼에 남는다 |
| `names`의 표시 이름 | 앱 `labels` |
| `programs.api_meta`, 어댑터의 `apply_end` | 카탈로그의 메타 + 앱 `fields[].from.meta` |
| `sources.base._VOLATILE`, `_ATTACHMENT_URLS`, 스냅샷의 `updtPnttm` | 카탈로그(출처 설명) |
| 어댑터 `to_record`의 본문 키 목록 | 앱 `sources[].read` |
| `notify.MASS_IMPACT`, `impact.REVIEW_THRESHOLD` | 앱 `alerts.hold` |
| `programs`의 재게시 묶기(`title_key`) | 카탈로그의 묶기 규칙, 앱은 `items.group`으로 고른다 |

옮기고 나면 ARCHITECTURE 9장의 "엔진·공통 안에 남은 지원사업 지식"이 없어진다.

## 9. 만드는 순서 (초안)

| 순서 | 할 일 | 완료 기준 |
|---|---|---|
| 1 | 앱 정의 스키마와 검증, 출처 카탈로그 설명. 지원사업 앱을 정의 파일 한 장으로 적고, 지금 코드가 그 파일에서 읽게 바꾼다(8장) | 동작이 그대로이고(기존 테스트 통과), 엔진·공통에 지원사업 낱말이 없다 |
| 2 | 본문 조각에 원본 키 적기, 앱이 읽는 키 기준으로 변경 거르기 | K-Startup 빈 버전 200개가 변경 목록에서 사라진다 |
| 3 | 미리 돌려 보기 | 지원사업 앱으로 지난 기간의 알림 목록이 나온다(LLM 없이는 마감일만) |
| 4 | 비교와 앱의 시험(맞다/틀리다 저장) | 판 두 개의 알림 차이와 깨진 시험이 나온다 |
| 5 | MCP 조종 도구: `list_sources`, `draft_app`, `validate_app`, `backtest`, `compare_app`, `enable_app` | 에이전트가 말로 받은 요구를 앱 정의로 만들고, 미리 돌려 보고, 켠다 |
| 6 | 두 번째 앱을 실제 출처로 올리고 규격을 확정한다 | 두 번째 앱이 플랫폼 코드를 거의 고치지 않고 돈다(RQ7) |

## 10. 열린 질문

| 질문 | 초안의 선택 | 정할 때 |
|---|---|---|
| 버전을 앱마다 따로 둘까, 출처 단위 하나에서 거를까 | 출처 단위 하나 + 앱별 거르기(5장) | 2번 |
| `watchlist`의 맞추기 같은 도메인 지식을 어디에 둘까 | 플랫폼 함수 목록에서 이름으로 고름 | 두 번째 앱 |
| 출처가 변경 내역을 직접 줄 때 엔진을 건너뛸까 | 미정 | 두 번째 앱 |
| 구독자 속성의 값은 어디에 둘까 | 속성 사전(이름·타입)은 앱 정의에, 값은 개인정보 금고에 | 금고 단계(10.1의 4번) |
| 가상 구독자로 미리 돌려 볼 때 가상 프로필을 어떻게 만들까 | 속성 사전에서 무작위(지금의 `eligibility.synthetic`과 같은 방식) | 3번 |
| 사용자 엔진(코드)과 앱 정의(설정)의 경계 | 설정으로 안 되는 계산만 엔진으로 | 빌더 단계 |
