# 수집원 약관·robots·공공누리 확인 (2026-10-05)

모든 항목은 2026-10-05에 페이지를 직접 열어 확인했다. 확인하지 못한 것은 "미확인"으로 적는다. 법률 자문이 아니며, 상용화 전에는 기관에 서면으로 확인한다.

## 1. K-Startup 사업공고 API (data.go.kr 15125364)

출처: https://www.data.go.kr/data/15125364/openapi.do

| 항목 | 내용 |
|---|---|
| 제공기관 | 창업진흥원 |
| 이용허락범위 | 제한 없음 |
| 승인 | 개발·운영 모두 자동승인 |
| 트래픽 | 개발계정 10,000 |
| 제공 필드 | 공고 본문(`pbanc_ctnt`), 상세 페이지 URL 등. **첨부파일 URL 필드는 없음** |
| 공공누리 | 공고 상세 페이지에 공공누리 제1유형(출처표시) 표기 |

## 2. 기업마당 지원사업정보 API

- 자체 API: https://www.bizinfo.go.kr/web/lay1/program/S1T175C174/apiDetail.do?id=bizinfoApi — `crtfcKey` 인증. 첨부 필드(`flpthNm`, `fileNm`, `printFlpthNm`) 포함. 트래픽 한도·이용조건은 미확인.
- data.go.kr 판: https://www.data.go.kr/data/15157820/openapi.do — 제공기관 중소벤처기업부. 같은 첨부 필드. **공공누리 제3유형(출처표시 + 변경금지)**. 운영 단계는 심의 승인.

## 3. robots.txt

| 사이트 | 확인 내용 |
|---|---|
| https://www.k-startup.go.kr/robots.txt | `Disallow: /afile*/`(첨부 다운로드 경로), `Disallow: /bizpbanc-ongoing.do` |
| https://www.bizinfo.go.kr/robots.txt | `/upload`, `/download`, `/common` 등만 차단. 공고 페이지와 첨부 경로 `/cmm/fms/`는 차단하지 않음 |

## 4. 약관·저작권정책

- K-Startup 이용약관 제14조②: 사전 승낙 없는 복제·제공 금지 (https://www.k-startup.go.kr/web/contents/webUTILZTN_STPL.do). 공고 페이지의 공공누리 1유형 표기와 부딪힌다.
- 기업마당 저작권정책: 콘텐츠로 직접 수익을 얻는 행위 금지, 사전 협의·출처 표시·무단 변경 금지 (https://www.bizinfo.go.kr/biz/bizd/selectBIZD120View.do). 공고 페이지에 공공누리 마크는 없음.
- 첨부파일(HWP/PDF)에 공공누리가 적용되는지는 두 사이트 모두 미확인.

## 5. 이 프로젝트의 결정

1. **웹 스크래핑은 하지 않는다.** data.go.kr의 두 API(15125364, 15157820)만 쓴다.
2. 본문은 K-Startup API, 첨부는 기업마당 API의 첨부 필드로 받는다.
3. 모든 화면·알림에 출처와 공공누리 유형을 표기한다.
4. 기업마당(변경금지): 원문 인용은 고치지 않고, LLM 요약은 "AI 요약"으로 원문과 분리해 표시한다. 요약이 "변경"에 해당하는지는 미확인.
5. 첨부 파일은 재배포하지 않고 원문 링크만 준다. 내부 분석용 저장은 최소화한다.
6. 상용화 전에는 창업진흥원·중소벤처기업부에 서면으로 확인한다.
