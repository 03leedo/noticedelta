"""파이프라인 워커: 새 스냅샷 적재 → 첨부 파싱 → 변경 기록 → (추출을 켜면) 영향 계산을 작업 큐로 돌린다.

    docker build -t noticedelta-parser infra/parser
    uv run python scripts/worker.py              # 할 일이 없을 때까지 처리하고 끝난다(LLM 없음)
    uv run python scripts/worker.py --status     # 작업 상태별 건수만 본다
    uv run python scripts/worker.py --backfill   # 변경 기록이 없는 사업 버전을 모두 등록
    uv run python scripts/worker.py --extract    # 영향 계산(LLM 추출)까지. 기본은 구독한 사업만
    uv run python scripts/worker.py --extract --scope changed --daily-tokens 60000
    uv run python scripts/worker.py --extract --extract-items 3   # 최근 정정된 사업 3개를 첫 판부터

Docker가 꺼져 있으면 적재만 하고 끝낸다. 파싱·비교 작업은 큐에 남아 다음 실행에서 이어진다.

추출(--extract)은 .env의 공급자(NOTICEDELTA_LLM_PROVIDER, 피클이면 무료 pickle-general)로 한다.
- 출처 값 필드(앱 정의의 from: meta, 마감)는 LLM에 묻지 않고 출처에서 읽는다.
- 같은 요청은 녹화본(data/cassettes)에서 꺼내 다시 보내지 않는다. 새로 보낸 호출도 녹화한다.
- 하루 한도(--daily-tokens, 한국 시간 자정 기준): 비행 기록기의 LLM 호출 기록으로 오늘 쓴 양을 세고,
  남은 만큼만 보낸다. 다 쓰면 영향 계산은 큐에 남아 다음 실행에서 이어진다.
- 범위(--scope): subscribed 구독자가 있는 사업(기본), changed 정정이 생긴 사업, all 모두.

DB는 .env의 DATABASE_URL(기본 sqlite:///data/dev.db). 아직 넣지 않은 실행이 있으면 적재 작업을
먼저 등록한다. 같은 작업은 한 번만 등록되므로 몇 번 돌려도 된다.
"""

from __future__ import annotations

import argparse
import os
import socket
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
from noticedelta import app_registry, budgets, flight  # noqa: E402
from noticedelta.agent import build_gateway  # noqa: E402
from noticedelta.blobstore import LocalBlobStore  # noqa: E402
from noticedelta.config import load_env, open_db  # noqa: E402
from noticedelta.db.models import ChangeEvent, ItemVersion  # noqa: E402
from noticedelta.drift import pending  # noqa: E402
from noticedelta.events import new_ulid  # noqa: E402
from noticedelta.jobs import counts, run_worker  # noqa: E402
from noticedelta.llm import FileCassetteStore, Gateway, Mode, choose, make_provider  # noqa: E402
from noticedelta.llm.extract import LLMExtractor, spec_for  # noqa: E402
from noticedelta.parsing import DockerParser  # noqa: E402
from noticedelta.pipeline import (  # noqa: E402
    IMPACT,
    LOAD,
    Context,
    enqueue_backfill,
    enqueue_item_impact,
    enqueue_load,
    enqueue_scope_backlog,
    flight_config,
    handlers,
)
from noticedelta.vault import load_cipher  # noqa: E402

KST = timezone(timedelta(hours=9))
DAILY_ENV = "NOTICEDELTA_LLM_DAILY_TOKENS"  # .env에서 읽으므로 load_env() 뒤에 읽는다


def _today_kst() -> datetime:
    now = datetime.now(KST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _recent_corrected(session, n: int) -> list[str]:
    """정정(둘째 판 이상)이 최근에 기록된 사업 n개."""
    rows = session.execute(
        sa.select(ItemVersion.item_id, sa.func.max(ChangeEvent.created_at))
        .join(ChangeEvent, ChangeEvent.to_version_id == ItemVersion.id)
        .where(ChangeEvent.from_version_id.is_not(None))
        .group_by(ItemVersion.item_id)
        .order_by(sa.func.max(ChangeEvent.created_at).desc())
        .limit(n)
    ).all()
    return [r[0] for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--max-jobs", type=int, default=None)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="변경 기록이 없는 버전을 모두 등록")
    ap.add_argument("--extract", action="store_true", help="영향 계산(LLM 추출)까지 돌린다")
    ap.add_argument("--scope", choices=["subscribed", "changed", "all"], default="subscribed")
    ap.add_argument(
        "--daily-tokens",
        type=int,
        default=None,
        help=f"하루에 보낼 LLM 토큰 한도(기본 {DAILY_ENV} 또는 200000)",
    )
    ap.add_argument(
        "--extract-items", type=int, default=0, help="최근 정정된 사업 n개를 첫 판부터 추출"
    )
    args = ap.parse_args()
    load_env()
    engine, factory = open_db()
    root = Path(args.root)

    with factory() as s:
        if not args.status:
            enqueue_load(s, root)
            if args.backfill:
                print(f"등록한 사업 버전: {enqueue_backfill(s)}")
            s.commit()
        print(f"작업 상태: {counts(s)}")
        held = pending(s)
        if held:
            # 출처가 형식을 바꾼 것으로 보여 정정으로 보지 않고 마지막 값을 이어 둔 키
            keys = ", ".join(f"{r.source}.{r.key}({r.kind} {r.changed}/{r.compared})" for r in held)
            print(f"형식 변화로 붙잡아 둔 키: {keys} · 결정은 scripts/drift.py")
    if args.status:
        return

    parser = DockerParser()
    ctx = Context(root=root, store=LocalBlobStore(root / "blobs"), parser=parser)
    ctx.vault = load_cipher(ROOT / "data" / "vault.key")  # 판정 값은 금고에서(동의 안에서)
    budget = gateway = usage_trace = None
    if args.extract:
        choice = choose()
        daily = (
            args.daily_tokens
            if args.daily_tokens is not None
            else int(os.environ.get(DAILY_ENV, "200000"))
        )
        now = datetime.now(UTC)
        # 소유자가 켠 다른 앱도 함께 돈다(영향 계산·알림은 앱마다 따로). 앱마다 하루 한도를 두고,
        # 그 위에 하루 전체 한도를 건다(budgets.py)
        with factory() as s:
            ctx.extra_apps = tuple(a for a in app_registry.enabled(s) if a.id != ctx.app.id)
            spent = flight.spent_tokens(s, _today_kst())
            budget, per_app = budgets.plan(
                s, ctx.apps(), primary=ctx.app.id, daily=daily, since=_today_kst()
            )
        provider = make_provider(choice)
        cassettes = FileCassetteStore(ROOT / "data" / "cassettes")
        gateways = {
            app_id: Gateway(
                provider,
                cassettes,
                Mode.REPLAY_OR_LIVE,
                budget=b,
                observer=flight.llm_observer,  # 묶음별 녹화본 색인(키·출력 해시)
            )
            for app_id, b in per_app.items()
        }
        gateway = gateways[ctx.app.id]
        ctx.extractor = LLMExtractor(gateway, model=choice.model, spec=spec_for(ctx.app))
        ctx.extractor_name = f"{choice.name}/{choice.model}"
        ctx.extract_scope = args.scope
        ctx.extractor_for = lambda app: LLMExtractor(
            gateways[app.id], model=choice.model, spec=spec_for(app)
        )
        if ctx.extra_apps:
            print("함께 도는 앱: " + ", ".join(f"{a.id}@{a.version}" for a in ctx.extra_apps))
        print(
            f"추출: {ctx.extractor_name} · 범위 {args.scope} · 오늘 쓴 토큰 {spent:,} / "
            f"한도 {daily:,} · 남은 {budget.limit:,} · 앱별 남은 "
            + ", ".join(f"{k} {b.limit:,}" for k, b in per_app.items())
        )
        with factory() as s:
            # 이번 실행의 묶음: 하루 한도에 드는 사용량(llm_usage)을 여기에 남긴다
            usage_trace = new_ulid()
            app_ref = f"app:{ctx.app.id}@{ctx.app.version}"
            flight.open_trace(
                s,
                usage_trace,
                kind="worker_run",
                started_at=now,
                config=flight_config(ctx),
                app=app_ref,
            )
            backlog = sum(enqueue_scope_backlog(s, ctx.for_app(a.id), now) for a in ctx.apps())
            if backlog:
                print(
                    f"범위 안인데 계산하지 않은 사업의 판 {backlog}개를 따라잡기로 넣었습니다(알림 없음)"
                )
            if args.extract_items:
                items = _recent_corrected(s, args.extract_items)
                carry = {"trace": usage_trace}
                n = sum(
                    enqueue_item_impact(s, i, carry, now, app_id=a.id)
                    for i in items
                    for a in ctx.apps()
                )
                print(
                    f"최근 정정된 사업 {len(items)}개의 판 {n}개를 새로 넣었습니다(따라잡기, 알림 없음)"
                )
            s.commit()
    elif args.extract_items:
        print("--extract-items는 --extract와 함께 써야 합니다")

    todo = handlers(ctx)
    if not parser.available():
        # 파서 없이 파싱 작업을 꺼내면 재시도 횟수만 쓰고 실패로 굳는다. 적재만 하고 나머지는 둔다.
        print("파서(Docker)를 쓸 수 없어 적재만 합니다. 파싱·비교는 다음 실행에서 이어집니다")
        todo = {LOAD: todo[LOAD]}
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    rest = {k: h for k, h in todo.items() if k != IMPACT}
    stats = run_worker(factory, rest, worker_id=worker_id, max_jobs=args.max_jobs)
    if args.extract and IMPACT in todo:
        logged = dict.fromkeys(gateways, 0)

        def log_usage() -> None:
            # 작업 트랜잭션과 따로 바로 커밋한다: 작업이 되돌려지거나, 호출이 실패하거나(공급자가 센
            # 토큰), 묶음 없는 작업이어도 하루 한도에 들어간다. 앱마다 ref를 달리 남긴다(앱별 예산)
            for app_id, gw in gateways.items():
                live = gw.usage.live_tokens
                if live <= logged[app_id]:
                    continue
                with factory() as s:
                    flight.add_record(
                        s,
                        usage_trace,
                        "llm_usage",
                        ref=app_registry.usage_ref(app_id, ctx.app.id),
                        at=datetime.now(UTC),
                        cost=live - logged[app_id],
                    )
                    s.commit()
                logged[app_id] = live

        # 영향 계산은 하나씩: 한도에 닿으면(blocked) 더 꺼내지 않는다. 한도에 걸린 작업은 시도 횟수를 쓰지 않고 미룬다
        # (앱 한도에 걸린 앱의 작업만 미뤄지고, 하루 전체 한도에 닿으면 모두 멈춘다)
        while not budget.blocked and budget.spent < budget.limit:
            if all(b.blocked or b.spent >= b.limit for b in per_app.values()):
                break
            if (
                args.max_jobs is not None
                and stats.done + stats.retried + stats.dead >= args.max_jobs
            ):
                break
            one = run_worker(factory, {IMPACT: todo[IMPACT]}, worker_id=worker_id, max_jobs=1)
            log_usage()
            stats.done, stats.retried, stats.dead = (
                stats.done + one.done,
                stats.retried + one.retried,
                stats.dead + one.dead,
            )
            if one.done + one.retried + one.dead == 0:
                break
        more = run_worker(factory, rest, worker_id=worker_id)  # 영향 계산이 낸 알림 발송
        stats.done, stats.retried, stats.dead = (
            stats.done + more.done,
            stats.retried + more.retried,
            stats.dead + more.dead,
        )
        log_usage()
        note = " · 한도에 닿아 나머지는 다음 실행으로 미룸" if budget.blocked else ""
        sent = sum(gw.usage.live_tokens for gw in gateways.values())
        print(f"이번에 보낸 LLM 토큰 {sent:,}{note}")
    with factory() as s:
        # 에이전트가 요청하고 보류 시간이 지난 효과를 실행한다(도구 게이트웨이, 많아야 한 번)
        acted = build_gateway(factory, cipher=ctx.vault).commit_due(s, now=datetime.now(UTC))
        if acted:
            print(f"보류가 끝나 실행한 에이전트 효과 {len(acted)}건")
    with factory() as s:
        # 남은 작업이 없는 수집 실행 묶음을 닫고 출력 해시를 적는다(scripts/replay.py가 비교한다)
        closed = flight.close_finished(s, datetime.now(UTC))
        s.commit()
        print(
            f"완료 {stats.done}, 재시도 예정 {stats.retried}, 실패 {stats.dead} · 작업 상태: {counts(s)}"
            + (f" · 닫은 묶음 {len(closed)}" if closed else "")
        )


if __name__ == "__main__":
    main()
