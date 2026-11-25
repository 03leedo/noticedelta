"""층 경계 (ARCHITECTURE 9장, 10.1). 아래층은 위층을 가져다 쓰지 않는다.

    공통  어느 서비스든 쓰는 것: 저장, 작업 큐, 파서, 수집, LLM 게이트웨이, 알림 발송
    엔진  변경 영향 엔진과 판정 규칙 엔진, 방식 비교 틀
    팩    도메인(지원사업): 출처 어댑터, 필드 정의와 프롬프트, 사업 묶기, 표시 이름, 라벨
    앱    화면과 조립(파이프라인)

새 모듈을 만들면 아래 표에 층을 적어야 한다. 폴더는 아직 나누지 않았고(두 번째 팩을 붙일 때 옮긴다),
이 테스트가 그때까지 경계를 지킨다.

지원사업 지식은 팩에만 있다: 필드 정의·낱말·자격 속성 사전은 grants, 출처 지식은 어댑터
(sources.bizinfo·kstartup), 표시 이름은 names. test_no_domain_words_in_platform이 지킨다.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "noticedelta"

# 낮은 층부터. 이름이 더 길게 맞는 쪽이 이긴다(db.repo는 엔진, 나머지 db는 공통).
LAYERS: dict[str, list[str]] = {
    "공통": [
        "blobstore",
        "catalog",  # 출처 한 장 규격(source-catalog.md)
        "config",
        "db",
        "document",
        "drift",  # 출처 형식 변화 감시
        "events",  # 이벤트 봉투, 타입 등록부, 구독 → 작업 큐
        "flight",  # 비행 기록기: 묶음과 기록, 출력의 정규형과 해시
        "jobs",
        "llm",  # gateway, cassette, harness, anthropic_provider
        "mcp_adapter",  # 등록부의 도구를 MCP로 내놓는다
        "metrics",
        "notify",
        "parsing",
        "people",  # 사람 ID와 개인 링크(토큰은 해시로만)
        "snapshot",
        "sources.attachments",
        "sources.base",
        "sources.http",
        "sources.resilience",
        "text",
        "tooling",  # 도구와 능력 속성, 등록부
        "usage",  # 에이전트 사용 기록과 피드백
        "vault",  # 개인정보 금고: 암호화한 값, 동의, 허가, 접근 기록
    ],
    "엔진": [
        "app_registry",  # 앱 정의의 원본(DB): 판 넣기, 켜기
        "backtest",  # 미리 돌려 보기와 판 비교(app-spec 10장)
        "appspec",  # 앱 정의 규격과 검증, 엔진이 쓰는 모양으로 바꾸기
        "citation",
        "doc_tools",  # 문서 읽기·두 판 비교·글 검사 도구
        "db.repo",  # 게시물 버전·알림을 엔진 타입으로 저장한다
        "eligibility",  # 규칙·판정·대량 판정(SQL)·시간에 따른 자격
        "extraction",  # 앱 정의로 만드는 LLM 필드 추출기(ExtractSpec)
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
        "grants",  # 필드 정의, 필드별 낱말, 자격 속성 사전
        "labeling",
        "llm.extract",  # 지원사업 필드 정의와 프롬프트
        "names",
        "programs",
        "sources",  # 패키지 머리(__init__)가 출처 어댑터를 함께 내보낸다
        "sources.bizinfo",
        "sources.kstartup",
    ],
    "앱": ["demo", "pipeline", "replay", "web"],  # replay: 같게 재생(파이프라인을 다시 돌린다)
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


# 플랫폼(공통·엔진)의 코드에 나오면 안 되는 지원사업 낱말. 주석·독스트링은 보지 않는다.
DOMAIN_WORDS = re.compile(
    r"bizinfo|kstartup|pblanc|pbanc|reqst|flpth|updtPnttm|inqireCo|기업마당|지원사업|업력|창업|"
    r"founded_on|ceo_|business_age|revenue_krw|region_sido|SIDO|eligibility_rules|"
    r"required_documents|\"deadline\"|\"contact\"|"  # "amount"는 값 타입(금액) 이름이라 뺀다
    r"[Pp]rogram"  # 플랫폼은 항목(item)을 다룬다. 사업(program)은 지원사업 앱의 낱말이다(2026-11-24)
)


def code_lines(path: Path):
    src = path.read_text(encoding="utf-8")
    docs = set()
    for node in ast.walk(ast.parse(src)):
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docs |= set(range(first.lineno, first.end_lineno + 1))
    for no, line in enumerate(src.splitlines(), 1):
        if no not in docs:
            yield no, line.split("#", 1)[0]


def test_no_domain_words_in_platform():
    found = [
        f"{name}:{no}: {code.strip()[:60]}"
        for name, path in modules()
        if layer_of(name) in ("공통", "엔진")
        for no, code in code_lines(path)
        if DOMAIN_WORDS.search(code) and not code.lstrip().startswith(("from ", "import "))
    ]
    assert found == []
