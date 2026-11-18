"""지원사업 팩: 지원사업 앱이 쓰는 도메인 지식을 한곳에 모은다.

플랫폼(공통·엔진)은 지원사업을 모른다. 여기서 넘겨준다: 필드 정의, 필드별 낱말, 자격 속성 사전.
출처 지식은 어댑터(sources/bizinfo.py, kstartup.py), 표시 이름은 names.py에 있다.
app-spec.md 13장의 2번 단계에서 이 모듈은 지원사업 앱의 정의 한 장(데이터)이 된다.
"""

from __future__ import annotations

from noticedelta.eligibility.rules import Attribute, Derive
from noticedelta.impact import keyword_detector

# 시·도의 짧은 이름 (region_sido 값)
SIDO = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북",
        "충남", "전북", "전남", "경북", "경남", "제주")  # fmt: skip

FIELD_TYPES: dict[str, str] = {
    "deadline": "datetime",
    "amount": "amount",
    "eligibility_rules": "rules",
    "required_documents": "list",
    "contact": "text",
}

FIELD_GUIDE: dict[str, str] = {
    "deadline": '신청 마감 일시. "YYYY-MM-DD HH:MM" 문자열 (시각이 없으면 "YYYY-MM-DD")',
    "amount": "기업당 최대 지원 금액. 원 단위 정수",
    "eligibility_rules": "신청 자격 규칙. 아래 규칙 형식의 JSON 객체",
    "required_documents": "제출 서류 이름 목록. 문자열 배열",
    "contact": "문의처. 문자열",
}

RULES_FIELD = "eligibility_rules"

# 필드별 낱말: 바뀐 조각이 어느 필드와 관련 있을지 고른다(impact.keyword_detector).
# 날짜·금액이 든 조각은 필드 타입으로 따로 고른다.
KEYWORDS: dict[str, str] = {
    # 날짜가 있는 조각은 find_datetimes가 따로 잡는다. "까지"만으로는 고르지 않는다.
    "deadline": r"마감|접수\s*기간|신청\s*기간|모집\s*기간|소진\s*시\s*까지",
    "amount": r"지원\s*금|지원\s*규모|지원\s*한도|사업비|최대\s*\d",
    RULES_FIELD: (
        r"신청\s*자격|지원\s*대상|자격\s*요건|제외\s*대상|업력|창업\s*\d+\s*년|만\s*\d+\s*세|"
        r"연령|나이|소재|예비\s*창업|재창업|여성|매출|상시\s*근로자|중복\s*수혜"
    ),
    # "서류 평가"처럼 서류만 들어간 문장은 고르지 않는다.
    "required_documents": (
        r"(?:제출|구비|증빙|첨부|신청|필요|준비)\s*서류|서류\s*(?:제출|목록)|증빙\s*자료|사본"
    ),
    "contact": r"문의|연락처|담당자|☎|\d{2,3}-\d{3,4}-\d{4}",
}

candidates = keyword_detector(KEYWORDS, FIELD_TYPES)

# 자격 속성 사전. 단위·배수·금액은 인용 검증이, derive는 시간에 따른 자격이 쓴다.
ATTRIBUTES: dict[str, Attribute] = {
    "is_pre_founder": Attribute(bool),
    "business_age_months": Attribute(
        int, units=("년", "개월"), scale=12, derive=Derive("months_since", "founded_on")
    ),
    "ceo_age": Attribute(int, units=("세",), derive=Derive("age", "ceo_birth_date")),
    "region_sido": Attribute(str, values=SIDO),
    "industry_ksic": Attribute(str),
    "business_type": Attribute(str, values=("individual", "corporation")),
    "revenue_krw": Attribute(int, money=True),
    "employees": Attribute(int, units=("명", "인")),
    "is_women_owned": Attribute(bool),
    "is_restart": Attribute(bool),
}
RULES_CONTEXT = {"attributes": ATTRIBUTES}  # RuleGroup.model_validate(..., context=RULES_CONTEXT)
