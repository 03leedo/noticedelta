"""출처 한 장(catalog)과 운영자 출처 두 장(sources/catalog/*.json)."""

import copy
import json
from pathlib import Path

import pytest

from noticedelta import grants
from noticedelta.appspec import check_app
from noticedelta.catalog import SourceSheet, check_sheet, load_catalog
from noticedelta.sources import CATALOG, bizinfo, kstartup

FIXTURES = Path(__file__).parent / "fixtures"
FOLDER = Path(bizinfo.__file__).parent / "catalog"


def test_operator_sources_load():
    assert set(load_catalog(FOLDER)) == set(CATALOG) == {"bizinfo", "kstartup"}
    assert CATALOG["bizinfo"].attachments is not None
    assert CATALOG["kstartup"].attachments is None  # 첨부 주소 필드가 없다


def test_adapters_see_the_same_knowledge_as_before():
    # 출처 한 장으로 옮기기 전(2026-11-24) 어댑터에 있던 값. 바뀌면 내용 해시가 바뀌어 가짜 변경이 난다
    assert bizinfo.IGNORE == kstartup.IGNORE == frozenset({"id", "totCnt", "inqireCo", "updtPnttm"})
    assert frozenset({"flpthNm", "printFlpthNm"}) == bizinfo.URL_KEYS
    assert bizinfo.DROP_PARAMS == ("downloadToken",)
    assert frozenset({"www.bizinfo.go.kr", "bizinfo.go.kr"}) == bizinfo.ATTACHMENT_HOSTS


def _raw_items(name: str) -> list[dict]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if "response" in data:
        items = data["response"]["body"]["items"]["item"]
    else:
        items = data["data"]
        items = items.get("data", []) if isinstance(items, dict) else items
    return items if isinstance(items, list) else [items]


@pytest.mark.parametrize(
    ("source", "fixture"), [("bizinfo", "bizinfo_page.json"), ("kstartup", "kstartup_page.json")]
)
def test_sheet_describes_every_key_the_source_sends(source, fixture):
    sheet = CATALOG[source]
    sent = set().union(*(item.keys() for item in _raw_items(fixture)))
    undescribed = sent - set(sheet.keys) - set(sheet.ignore.keys)
    assert undescribed == set(), f"keys에 설명을 적는다: {sorted(undescribed)}"


def test_adapters_read_only_described_keys():
    body = {k for _, k in kstartup._BODY_FIELDS} | {
        "biz_pbanc_nm",
        "pbanc_rcpt_bgng_dt",
        "pbanc_rcpt_end_dt",
    }
    assert body <= CATALOG["kstartup"].readable_keys()
    used = {"pblancNm", "bsnsSumryCn", "trgetNm", "reqstBeginEndDe", "reqstMthPapersCn", "refrncNm"}
    assert used <= CATALOG["bizinfo"].readable_keys()


BASE = json.loads((FOLDER / "bizinfo.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d["item"].update(id="nttId"), "item.id: keys에 없는 키 nttId"),
        (
            lambda d: d["meta"]["apply_end"].update(key="endDe"),
            "meta.apply_end.key: keys에 없는 키 endDe",
        ),
        (lambda d: d["ignore"]["keys"].append("pblancId"), "항목 ID pblancId를 무시하면"),
        (
            lambda d: d["attachments"]["urls"].append("fileUrl"),
            "attachments: keys에 없는 키 fileUrl",
        ),
        (lambda d: d["connector"].update(kind="html_list"), "connector.kind"),
    ],
)
def test_sheet_problems(change, expected):
    data = copy.deepcopy(BASE)
    change(data)
    sheet, problems = check_sheet(data)
    assert sheet is None
    assert any(expected in str(p) for p in problems), [str(p) for p in problems]


def test_app_may_read_only_what_the_catalog_offers():
    keys = {sid: s.readable_keys() for sid, s in CATALOG.items()}
    meta = {sid: set(s.meta) for sid, s in CATALOG.items()}
    app = json.loads(grants.DEFINITION.read_text(encoding="utf-8"))
    assert check_app(app, sources=keys, meta=meta)[1] == []

    ignored = copy.deepcopy(app)
    ignored["sources"][0]["read"].append(
        "inqireCo"
    )  # 조회수: 무시하는 키라 읽어도 변경이 오지 않는다
    assert any("inqireCo" in str(p) for p in check_app(ignored, sources=keys, meta=meta)[1])

    no_meta = copy.deepcopy(app)
    no_meta["engine"]["fields"]["deadline"] = {"type": "datetime", "from": {"meta": "apply_close"}}
    found = [str(p) for p in check_app(no_meta, sources=keys, meta=meta)[1]]
    assert any("구조화 값 apply_close를 주지 않는다" in p and "apply_end" in p for p in found), (
        found
    )


def test_sheet_schema_is_published():
    path = Path(__file__).resolve().parents[1] / "docs" / "source-schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == SourceSheet.model_json_schema()
