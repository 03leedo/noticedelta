"""층 경계 (ARCHITECTURE 9장, 10.1). 아래층은 위층을 가져다 쓰지 않는다.

    공통  어느 서비스든 쓰는 것: 저장, 작업 큐, 파서, 수집, LLM 게이트웨이, 알림 발송
    엔진  변경 영향 엔진과 판정 규칙 엔진, 방식 비교 틀
    팩    도메인(지원사업): 출처 어댑터, 필드 정의와 프롬프트, 사업 묶기, 표시 이름, 라벨
    앱    화면과 조립(파이프라인)

새 모듈을 만들면 아래 표에 층을 적어야 한다. 폴더는 아직 나누지 않았고(두 번째 팩을 붙일 때 옮긴다),
이 테스트가 그때까지 경계를 지킨다.

가져다 쓰지는 않지만 엔진·공통 안에 남아 있는 지원사업 지식 (팩 규약을 정할 때 옮긴다):
- eligibility.rules의 속성 사전(ATTRIBUTES, SIDO)과 eligibility.temporal의 속성 계산(업력, 나이)
- impact의 낱말 목록(_KEYWORDS)과 규칙 필드 이름(RULES_FIELD)
- sources.base의 변동 값 이름(_VOLATILE, _ATTACHMENT_URLS)과 snapshot의 updtPnttm
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "noticedelta"

# 낮은 층부터. 이름이 더 길게 맞는 쪽이 이긴다(db.repo는 엔진, 나머지 db는 공통).
LAYERS: dict[str, list[str]] = {
    "공통": [
        "blobstore",
        "config",
        "db",
        "document",
        "jobs",
        "llm",  # gateway, cassette, harness, anthropic_provider
        "mcp_adapter",  # 등록부의 도구를 MCP로 내놓는다
        "metrics",
        "notify",
        "parsing",
        "snapshot",
        "sources.attachments",
        "sources.base",
        "sources.http",
        "sources.resilience",
        "text",
        "tooling",  # 도구와 능력 속성, 등록부
    ],
    "엔진": [
        "citation",
        "db.repo",  # 게시물 버전·알림을 엔진 타입으로 저장한다
        "eligibility",  # 규칙·판정·대량 판정(SQL)·시간에 따른 자격
        "evaluation",
        "history",
        "impact",
        "ingest",
        "item_tools",  # 항목·변경을 조회하는 읽기 도구
        "matching",
        "segments",
        "values",
    ],
    "팩": [
        "eligibility.synthetic",  # 가상 프로필과 예시 규칙
        "labeling",
        "llm.extract",  # 지원사업 필드 정의와 프롬프트
        "names",
        "programs",
        "sources",  # 패키지 머리(__init__)가 출처 어댑터를 함께 내보낸다
        "sources.bizinfo",
        "sources.kstartup",
    ],
    "앱": ["demo", "pipeline", "web"],
}
ORDER = list(LAYERS)


def layer_of(module: str) -> str | None:
    best: tuple[str, str] | None = None
    for layer, names in LAYERS.items():
        for name in names:
            if (module == name or module.startswith(name + ".")) and (
                best is None or len(name) > len(best[1])
            ):
                best = (layer, name)
    return best[0] if best else None


def modules():
    for path in sorted(ROOT.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        name = name.removesuffix(".__init__")
        if name != "__init__":
            yield name, path


def imported(path: Path) -> set[str]:
    """이 파일이 가져다 쓰는 noticedelta 모듈 이름들 (가져온 이름까지 붙인 후보 포함)."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name[12:] for a in node.names if a.name.startswith("noticedelta.")}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("noticedelta"):
            base = node.module[len("noticedelta") :].lstrip(".")
            names = [a.name for a in node.names]
            out |= {f"{base}.{n}" for n in names} | {base} if base else set(names)
    return out


def test_every_module_has_a_layer():
    assert [name for name, _ in modules() if layer_of(name) is None] == []


def test_lower_layers_do_not_import_upper_layers():
    wrong = []
    for name, path in modules():
        mine = ORDER.index(layer_of(name))
        for target in sorted(imported(path)):
            theirs = layer_of(target)
            if theirs is not None and ORDER.index(theirs) > mine:
                wrong.append(f"{name}({ORDER[mine]}) → {target}({theirs})")
    assert wrong == []
