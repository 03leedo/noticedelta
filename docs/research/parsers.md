# HWP·HWPX·PDF 파서 품질 비교 (2026-10-19)

> 작업 목록 3번. 설계 15.1 확인 항목 4("첨부 파싱 성공률 80% 이상")의 진행 판단 근거.
> 원본 첨부는 `data/`(gitignore)에만 두고 저장소에는 올리지 않는다. 결과 수치만 남긴다.

## 결론

- **진행(Go).** 기본 파서 kordoc으로 첨부 119개 중 **112개(94%)** 를 파싱했다. HWP·HWPX 70개는 **전부** 성공했고, 실패 7개는 모두 글자를 뽑을 수 없는 PDF(스캔본 6, 글꼴 대응표가 깨진 PDF 1)다.
- **기본 파서: kordoc 4.18.13** (Node, MIT). 세 형식을 한 파서로 처리하고, 공고문 본 파일 60개를 모두 성공했다.
- **보조 파서: hwp-hwpx-parser 1.0.0** (Python, Apache-2.0). HWPX 표 개수가 원본 XML과 정확히 맞는다. kordoc이 실패하거나 표 구조를 따로 확인할 때 쓴다.
- **텍스트 층 없는 PDF**는 파서 문제가 아니다. `needs_review`로 보내고 OCR은 나중에 따로 다룬다(설계 7.3).

## 표본

기업마당 API에서 최신 공고 60건을 받아 첨부를 모두 내려받았다(2026-10-19, `scripts/ingest_live.py`).

| 형식(내용으로 판별) | 개수 | 비교 대상 |
|---|---|---|
| PDF | 49 | O |
| HWP 5 | 37 + 3 | O (3개는 확장자가 `.hwpx`인 HWP 5) |
| HWPX | 30 | O |
| ZIP 묶음 | 9 | X (안에 신청서 서식 HWP·HWPX가 들어 있음. 풀기는 따로) |
| DOCX, XLSX | 각 1 | X |
| **합계** | 130 | 119 |

역할로 보면 공고문 본 파일(`printFileNm`) 60개, 붙임 서식 70개다.

## 후보

| 파서 | 버전 | 언어·라이선스 | 형식 | 비고 |
|---|---|---|---|---|
| kordoc | 4.18.13 | Node, MIT | HWP·HWPX·PDF | 2026-10-04 배포판. 최신판(4.22.13, 10-18)은 공개된 지 2주가 안 돼 쓰지 않았다. 릴리스가 211개로 매우 잦다 |
| hwp-hwpx-parser | 1.0.0 | Python, Apache-2.0 | HWP·HWPX | 확장자로 형식을 고르므로 해시 이름 파일에 확장자를 붙여 넘긴다 |
| pdfplumber | 0.11.10 | Python, MIT | PDF | PDF 기준선 |
| rhwp-python | 0.8.1 | Rust 바인딩, MIT | HWP·HWPX | **제외.** 배포된 휠이 `FT_Palette_Data_Get`(freetype)을 찾지 못해 import 실패. Debian bookworm·trixie(freetype 2.13.3) 모두 같음 |

## 방법

- **격리 실행** (`infra/parser-bench/`): 네트워크 없음, 읽기 전용 루트, `/tmp`만 쓰기 가능, 메모리 1GB·CPU 1개·프로세스 256개 제한, 권한 전부 제거, 비루트. 파일마다 별도 프로세스로 돌리고 60초가 넘으면 끊는다. 설계 11장의 격리 파서 조건을 그대로 쓴 것이다.
- **기준 텍스트** (파서와 무관하게 원본에서 직접 뽑음)
  - HWPX: `Contents/section*.xml`의 `<hp:t>` 텍스트 전체와 `<hp:tbl>` 개수
  - HWP 5: OLE의 `PrvText` 스트림. 한글이 저장할 때 남기는 문서 앞부분 미리보기 텍스트다.
  - PDF: 기준 텍스트 없음. 두 파서를 서로 비교한다.
- **성공 기준** (`scripts/compare_parsers.py`)
  1. 오류나 시간 초과 없이 끝남
  2. 표기용 마크업(`<br>`, 표 구분선)을 지운 뒤 공백을 뺀 글자가 50자 이상이고 한글 비율이 30% 이상
  3. 기준 텍스트가 있으면, 기준 텍스트의 문자 3-gram 중 80% 이상이 출력에 있음

## 결과

| 파서 | 형식 | 파일 | 오류 없이 끝남 | 성공 | 기준 재현율 중앙값 | 표 개수 = XML | 시간 중앙값 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| kordoc | HWP | 40 | 40 | **40 (100%)** | 1.000 | - | 62 / 166 |
| hwp-hwpx-parser | HWP | 40 | 39 | 38 (95%) | 0.996 | - | 122 / 215 |
| kordoc | HWPX | 30 | 30 | **30 (100%)** | 0.994 | 11/30 | 696 / 2279 |
| hwp-hwpx-parser | HWPX | 30 | 30 | **30 (100%)** | 0.994 | **30/30** | 132 / 403 |
| kordoc | PDF | 49 | 49 | 42 (86%) | - | - | 2307 / 8566 |
| pdfplumber | PDF | 49 | 49 | 42 (86%) | - | - | 1457 / 12458 |

HWP 행은 확장자가 `.hwpx`였던 HWP 5 3개를 더한 수다(그 3개는 두 파서 모두 성공).

- **공고문 본 파일**: kordoc 60/60. hwp-hwpx-parser는 HWP·HWPX 본 파일 23개 중 22개(하나는 60초 초과). pdfplumber는 PDF 본 파일 37/37.
- **PDF 실패 7개**는 두 파서가 같다. 6개는 텍스트 층이 없는 스캔본이고, 1개는 글자는 나오지만 한글이 하나도 없다(글꼴 대응표가 깨진 PDF). 스캔본을 빼면 두 파서 모두 42/43이다.
- **hwp-hwpx-parser 실패 2개**: 380KB HWP 하나는 오류 없이 빈 결과를 냈고, 160KB 공고문 하나는 60초를 넘겼다. 두 파일 모두 kordoc은 성공했다.

### 표 구조

| | 원본 XML (HWPX 30개) | kordoc | hwp-hwpx-parser |
|---|---|---|---|
| HWPX 표 개수 합 | 606 | 562 | 606 |
| HWP 표 개수 합 (40개) | - | 589 | 669 |
| PDF 표 개수 합 (49개) | - | 545 | 675 (pdfplumber) |

- kordoc은 HWPX 30개 중 15개에서 XML보다 표를 적게, 4개에서 많이 냈다. 칸 하나짜리 장식 표(예: "붙임1" 상자)를 일반 문단으로 푸는 동작이 주원인이다(표본 확인). 칸 안의 줄바꿈은 `<br>`로 보존한다.
- hwp-hwpx-parser는 XML의 표를 그대로 다 내지만 칸 안의 줄을 공백으로 합친다.
- **변경 감지에는 kordoc 쪽이 유리하다.** 장식 표가 문단이 되고 칸 안 줄이 살아 있으면 조각이 더 의미 단위에 가깝다. 다만 "표 머리글 + 행 키"로 표를 맞추는 조각 맞추기(설계 7.4)가 장식 표를 표로 기대하지 않도록 조각화 연결(작업 목록 4번)에서 확인한다.

### 처리 시간

kordoc의 HWPX가 중앙값 0.7초, p95 2.3초로 가장 느리다. 공고 하나에 첨부가 2~3개라 수집 주기(시간 단위)에 비해 문제는 없지만, 백필(설계 9.5) 때는 파서 워커를 늘려야 한다.

## 채점에서 바로잡은 것

첫 집계에서 kordoc HWP 성공률이 73%로 나왔다. kordoc이 칸 안 줄바꿈을 `<br>`로 쓰는데, 정규화에서 `br` 글자가 한글 사이에 남아 3-gram을 끊고 한글 비율도 낮췄기 때문이다. 표기용 마크업을 지운 뒤 채점하도록 고쳤고, 실제로 빠진 줄이 없음을 줄 단위로 확인했다. 또 hwp-hwpx-parser가 확장자로 형식을 고르는 것, kordoc의 PDF 처리 중 pdfjs가 표준 출력에 경고를 섞는 것도 실행기에서 처리했다.

## 정한 것

1. **조각화 연결(작업 목록 4번)은 kordoc의 구조화 출력(`blocks`)을 쓴다.** Markdown이 아니라 블록(문단·제목·표, 표는 셀 2차원 배열)을 받아 `segments.py`의 조각과 위치(`locator`)로 바꾼다.
2. **파서는 격리 컨테이너 안의 Node 서비스로 둔다.** 입력은 원문 저장소 키, 출력은 JSON뿐이다(설계 7.3). 파이프라인이 Python이어도 경계가 JSON이라 문제가 없다.
3. **kordoc이 실패하면 HWP·HWPX는 hwp-hwpx-parser로 한 번 더 시도**하고, 그래도 실패하면 `needs_review`.
4. **텍스트 층이 없는 PDF는 `needs_review`.** OCR(kordoc 선택 의존성 또는 별도 서비스)은 비용·정확도를 따로 재고 나서 붙인다.
5. **kordoc 버전은 고정하고, 올릴 때는 이 비교를 다시 돌린다.** 릴리스가 잦아(2026-10 기준 211개) 같은 표본으로 회귀를 확인하는 편이 안전하다.
6. **ZIP 묶음 첨부(표본의 7%)는 다음에 다룬다.** 압축 폭탄·경로 조작을 막는 제한(파일 수, 전체 크기, 경로 정규화)을 둔 뒤 안의 HWP·HWPX를 같은 파서로 돌린다.

## 재현

```bash
uv run python scripts/ingest_live.py --sources bizinfo --per-source 60
uv run python scripts/compare_parsers.py prepare
bash infra/parser-bench/run.sh data/parser-bench/in data/parser-bench/out
uv run python scripts/compare_parsers.py report
```

(`ingest_live.py`는 `DATABASE_URL=sqlite:///data/corpus.db`, `NOTICEDELTA_BLOB_BACKEND=local`, `NOTICEDELTA_BLOB_DIR=data/corpus-blobs`로 실행했다.)
