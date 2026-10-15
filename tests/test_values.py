from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from noticedelta.values import (
    find_amounts,
    find_datetimes,
    parse_krw,
    parse_kst_datetime,
    values_equal,
)

KST = ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("최대 1억 원", 100_000_000),
        ("5,000만원", 50_000_000),
        ("3천만 원", 30_000_000),
        ("1억 5천만원", 150_000_000),
        ("100,000,000원", 100_000_000),
        ("2.5억원", 250_000_000),
        ("500만 원 이내", 5_000_000),
    ],
)
def test_parse_krw(text, expected):
    assert parse_krw(text) == expected


def test_parse_krw_without_amount():
    assert parse_krw("지원 규모 미정") is None


def test_find_amounts_in_sentence():
    text = "기업당 최대 1억원(정부지원금 7,000만원, 자부담 3천만 원)"
    assert find_amounts(text) == [100_000_000, 70_000_000, 30_000_000]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026. 10. 31.(금) 18:00", datetime(2026, 10, 31, 18, 0, tzinfo=KST)),
        ("2026-10-31", datetime(2026, 10, 31, 23, 59, tzinfo=KST)),
        ("'26.10.31", datetime(2026, 10, 31, 23, 59, tzinfo=KST)),
        ("2026년 10월 31일 18시", datetime(2026, 10, 31, 18, 0, tzinfo=KST)),
        ("2026년 10월 31일(금) 오후 6시", datetime(2026, 10, 31, 18, 0, tzinfo=KST)),
        ("20261031", datetime(2026, 10, 31, 23, 59, tzinfo=KST)),
    ],
)
def test_parse_kst_datetime(text, expected):
    assert parse_kst_datetime(text) == expected


def test_month_day_needs_default_year():
    assert parse_kst_datetime("10월 31일") is None
    assert parse_kst_datetime("10월 31일", default_year=2026) == datetime(2026, 10, 31, 23, 59, tzinfo=KST)
    assert parse_kst_datetime("10. 31.(금)", default_year=2026) == datetime(2026, 10, 31, 23, 59, tzinfo=KST)


def test_find_datetimes_in_sentence():
    text = "접수기간: 2026. 10. 13.(월) ~ 2026. 10. 31.(금) 18:00까지"
    assert find_datetimes(text) == [
        datetime(2026, 10, 13, 23, 59, tzinfo=KST),
        datetime(2026, 10, 31, 18, 0, tzinfo=KST),
    ]


def test_values_equal_datetime_across_formats():
    assert values_equal("datetime", "10월 31일", "10. 31.", default_year=2026)
    assert not values_equal("datetime", "10월 31일", "11월 7일", default_year=2026)


def test_values_equal_amount_across_formats():
    assert values_equal("amount", "1억원", "100,000,000원")
    assert values_equal("amount", 100_000_000, "1억 원")


def test_values_equal_rules_ignores_order_and_source():
    a = {"group": "all_of", "rules": [
        {"attribute": "ceo_age", "op": "<=", "value": 39, "source": "L1"},
        {"attribute": "business_age_months", "op": "<=", "value": 84, "source": "L2"},
    ]}
    b = {"group": "all_of", "rules": [
        {"attribute": "business_age_months", "op": "<=", "value": 84, "source": "L9"},
        {"attribute": "ceo_age", "op": "<=", "value": 39, "source": "L8"},
    ]}
    c = {"group": "all_of", "rules": [
        {"attribute": "business_age_months", "op": "<=", "value": 60, "source": "L9"},
        {"attribute": "ceo_age", "op": "<=", "value": 39, "source": "L8"},
    ]}
    assert values_equal("rules", a, b)
    assert not values_equal("rules", a, c)


def test_values_equal_list_ignores_order_and_spacing():
    assert values_equal("list", ["사업계획서", "사업자등록증  사본"], ["사업자등록증 사본", "사업계획서"])
    assert not values_equal("list", ["사업계획서"], ["사업계획서", "재무제표"])
