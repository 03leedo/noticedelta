from noticedelta.segments import Table, normalize_text, segment_document, text_hash


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  신청\t자격 \n 안내  ") == "신청 자격 안내"


def test_whitespace_only_difference_has_same_hash():
    assert text_hash("업력 7년  이내") == text_hash("업력 7년 이내")
    assert text_hash("업력 7년 이내") != text_hash("업력 5년 이내")


def test_blank_lines_split_paragraphs():
    segs = segment_document(["첫 문단입니다.\n이어지는 줄.\n\n둘째 문단입니다."], source="body")
    assert [s.kind for s in segs] == ["paragraph", "paragraph"]
    assert segs[0].text == "첫 문단입니다. 이어지는 줄."
    assert segs[0].locator == {"source": "body", "block": 0}


def test_short_marker_line_is_heading():
    segs = segment_document(["□ 신청 자격\n\n○ 공고일 기준 업력 7년 이내 기업"], source="body")
    assert [s.kind for s in segs] == ["heading", "paragraph"]


def test_list_items_become_separate_segments():
    segs = segment_document(
        ["○ 업력 7년 이내\n○ 대표자 만 39세 이하\n  (공고일 기준)"], source="body"
    )
    assert [s.text for s in segs] == ["○ 업력 7년 이내", "○ 대표자 만 39세 이하 (공고일 기준)"]


def test_table_cells_with_locator():
    table = Table(rows=[["구분", "내용"], ["업력", "7년 이내"], ["지원금", "최대 1억원"]])
    segs = segment_document(["안내", table], source="att:1")
    cells = [s for s in segs if s.kind == "table_cell"]
    assert len(cells) == 4
    assert cells[1].text == "7년 이내"
    assert cells[1].locator == {
        "source": "att:1",
        "table": 0,
        "row": 1,
        "col": 1,
        "row_key": "업력",
        "header": "내용",
        "table_sig": "구분|내용",
    }


def test_order_is_contiguous_from_zero():
    table = Table(rows=[["a", "b"], ["1", "2"]])
    segs = segment_document(["x\n\ny", table, "z"], source="body")
    assert [s.order for s in segs] == list(range(len(segs)))
    assert all(s.lineage_id is None for s in segs)
