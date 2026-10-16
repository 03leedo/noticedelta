import itertools

from noticedelta.matching import MatchMode, match_segments, similarity
from noticedelta.segments import Table, segment_document


def ids():
    counter = itertools.count(1)
    return lambda: f"L{next(counter)}"


def first_version(blocks, new_id):
    return match_segments([], segment_document(blocks, source="body"), new_id=new_id).segments


def test_first_version_all_added():
    new_id = ids()
    cs = match_segments([], segment_document(["a문단\n\nb문단"], source="body"), new_id=new_id)
    assert cs.added == ["L1", "L2"]
    assert [s.lineage_id for s in cs.segments] == ["L1", "L2"]


def test_no_change_keeps_lineage():
    new_id = ids()
    old = first_version(["공고 개요\n\n신청 자격 안내"], new_id)
    cs = match_segments(
        old, segment_document(["공고 개요\n\n신청 자격 안내"], source="body"), new_id=new_id
    )
    assert cs.unchanged == ["L1", "L2"]
    assert cs.added == cs.removed == cs.modified == []


def test_modified_paragraph_keeps_lineage():
    new_id = ids()
    old = first_version(["공고 개요\n\n접수 마감은 2026년 10월 31일 18시까지입니다."], new_id)
    new = segment_document(
        ["공고 개요\n\n접수 마감은 2026년 11월 7일 18시까지입니다."], source="body"
    )
    cs = match_segments(old, new, new_id=new_id)
    assert cs.modified == ["L2"]
    assert cs.unchanged == ["L1"]
    assert cs.segments[1].lineage_id == "L2"


def test_insert_and_delete():
    new_id = ids()
    old = first_version(["가 문단입니다\n\n나 문단입니다\n\n다 문단입니다"], new_id)
    new = segment_document(
        ["가 문단입니다\n\n다 문단입니다\n\n완전히 새로 추가된 안내 사항"], source="body"
    )
    cs = match_segments(old, new, new_id=new_id)
    assert cs.removed == ["L2"]
    assert cs.added == ["L4"]
    assert cs.unchanged == ["L1", "L3"]


def test_duplicate_sentences_keep_own_lineage():
    new_id = ids()
    old = first_version(
        ["※ 문의는 담당자에게\n\n중간 문단 원래 내용\n\n※ 문의는 담당자에게"], new_id
    )
    new = segment_document(
        ["※ 문의는 담당자에게\n\n중간 문단 바뀐 내용\n\n※ 문의는 담당자에게"], source="body"
    )
    cs = match_segments(old, new, new_id=new_id)
    assert [s.lineage_id for s in cs.segments] == ["L1", "L2", "L3"]
    assert cs.unchanged == ["L1", "L3"]
    assert cs.modified == ["L2"]


def test_hash_mode_treats_edit_as_remove_and_add():
    new_id = ids()
    old = first_version(["접수 마감은 2026년 10월 31일 18시까지입니다."], new_id)
    new = segment_document(["접수 마감은 2026년 11월 7일 18시까지입니다."], source="body")
    cs = match_segments(old, new, mode=MatchMode.HASH, new_id=new_id)
    assert cs.removed == ["L1"]
    assert cs.added == ["L2"]
    assert cs.modified == []


def test_table_cell_change_is_modified():
    new_id = ids()
    old = first_version(
        [Table([["구분", "내용"], ["업력", "7년 이내"], ["지원금", "최대 1억원"]])], new_id
    )
    new = segment_document(
        [Table([["구분", "내용"], ["업력", "5년 이내"], ["지원금", "최대 1억원"]])], source="body"
    )
    cs = match_segments(old, new, new_id=new_id)
    assert cs.modified == ["L2"]
    assert sorted(cs.unchanged) == ["L1", "L3", "L4"]


def test_table_row_reorder_is_unchanged():
    new_id = ids()
    old = first_version(
        [Table([["구분", "내용"], ["업력", "7년 이내"], ["지원금", "최대 1억원"]])], new_id
    )
    new = segment_document(
        [Table([["구분", "내용"], ["지원금", "최대 1억원"], ["업력", "7년 이내"]])], source="body"
    )
    cs = match_segments(old, new, new_id=new_id)
    assert cs.modified == cs.added == cs.removed == []
    by_text = {s.text: s.lineage_id for s in cs.segments}
    assert by_text["7년 이내"] == "L2"


def test_similarity_bounds():
    assert similarity("업력 7년 이내", "업력 7년 이내") == 1.0
    assert similarity("업력 7년 이내", "전혀 다른 문장입니다") < 0.2
