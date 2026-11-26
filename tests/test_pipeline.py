"""작업 종류로 이은 파이프라인: 적재 → 파싱 → 변경 기록 (작업 목록 13번)."""

import itertools
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_history import DOCS, RULES, ByContent, pdf

from noticedelta import people, vault
from noticedelta.blobstore import LocalBlobStore
from noticedelta.db import make_engine, models
from noticedelta.jobs import counts, run_worker
from noticedelta.parsing import ParserUnavailable
from noticedelta.pipeline import Context, enqueue_load, handlers
from noticedelta.snapshot import run_snapshot
from noticedelta.sources.bizinfo import to_record

HOST = "https://www.bizinfo.go.kr"


class Clock:
    def __init__(self):
        # 기본은 지금보다 조금 뒤. 작업 큐도 이 시계로 등록하므로 과거 날짜로 바꿔도 된다.
        self.t = datetime.now(UTC) + timedelta(minutes=1)

    def __call__(self):
        return self.t


class Down:
    def parse(self, data, mime):
        raise ParserUnavailable("docker 꺼짐")


class Env:
    def __init__(self, tmp_path):
        engine = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
        models.Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine)
        self.root = tmp_path / "snapshots"
        self.files: dict[str, bytes | None] = {}
        self.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (
                    httpx.Response(200, content=self.files[r.url.path])
                    if self.files.get(r.url.path)
                    else httpx.Response(503)
                )
            )
        )
        self.hours = itertools.count()
        self.clock = Clock()
        self.ctx = Context(
            root=self.root,
            store=LocalBlobStore(self.root / "blobs"),
            parser=ByContent(),
            now=self.clock,
        )

    def collect(self, deadline, attachment):
        self.files["/f/a.pdf"] = attachment
        raw = {
            "pblancId": "P1",
            "pblancNm": "2026년 (가상) 창업 지원 모집",
            "excInsttNm": "기관X",
            "reqstBeginEndDe": f"2026-10-13 ~ {deadline}",
            "bsnsSumryCn": f"<p>접수 마감 {deadline}</p>",
            "fileNm": "공고문.pdf",
            "flpthNm": f"{HOST}/f/a.pdf",
        }
        t = datetime(2026, 10, 23) + timedelta(hours=6 * next(self.hours))
        run_snapshot(
            {"bizinfo": lambda page: [to_record(raw)] if page == 1 else []},
            http=self.http,
            root=self.root,
            now=lambda: t,
        )
        with self.factory() as s:
            enqueue_load(s, self.root, self.clock())
            s.commit()

    def work(self, parser=None):
        if parser is not None:
            self.ctx.parser = parser
        return run_worker(self.factory, handlers(self.ctx), worker_id="w", now=self.clock)

    def count(self, model):
        with self.factory() as s:
            return s.scalar(sa.select(sa.func.count()).select_from(model))

    def jobs(self):
        with self.factory() as s:
            return counts(s)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_runs_flow_from_load_to_change_events(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    stats = env.work()
    # 적재 2 + 파싱 2 + 비교 2
    assert (stats.done, stats.retried, stats.dead) == (6, 0, 0)
    assert env.count(models.LoadedRun) == 2
    assert env.count(models.ChangeEvent) == 2
    with env.factory() as s:
        ev = s.scalars(
            sa.select(models.ChangeEvent).where(models.ChangeEvent.from_version_id.is_not(None))
        ).one()
        assert ev.changed_lineages["modified"]


def test_nothing_new_means_no_new_jobs(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        assert enqueue_load(s, env.root) is None
    assert env.work().done == 0


def test_parser_outage_is_retried_later(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    stats = env.work(parser=Down())
    assert stats.retried == 1 and env.count(models.ChangeEvent) == 0
    env.clock.t += timedelta(hours=1)  # 재시도 시각이 지나고 파서가 돌아옴
    env.work(parser=ByContent())
    assert env.count(models.ChangeEvent) == 1
    assert env.jobs() == {"done": 3}  # 적재·파싱·비교, 파싱은 재시도 뒤 성공


def test_late_attachment_is_filled_and_compared(env):
    """첫 수집에서 첨부 받기가 실패했다. 다음 수집에서 받으면 같은 버전을 다시 파싱해 기록한다."""
    env.collect("2026-11-07", None)  # 503
    env.work()
    assert env.count(models.ChangeEvent) == 0  # 첨부가 없어 비교를 기다린다
    assert env.jobs().get("queued") == 1  # 비교는 30분 뒤로 미뤄 둠

    env.collect("2026-11-07", pdf(RULES, DOCS))  # 내용은 그대로, 첨부만 이번에 받음
    env.clock.t += timedelta(hours=1)
    env.work()
    assert env.count(models.ChangeEvent) == 1
    assert env.count(models.ItemVersion) == 1


class RulesFromText:
    """“창업 N년 이내”를 자격 규칙으로 뽑는 가짜 추출기 (LLM 대신)."""

    def __init__(self, confidence=1.0):
        self.confidence = confidence
        self.calls = []

    def __call__(self, fields, segments):
        import re

        from noticedelta.impact import FieldValue

        self.calls.append(sorted(fields))
        for s in segments:
            m = re.search(r"창업 (\d+)년", s.text)
            if m and "eligibility_rules" in fields:
                rules = {
                    "group": "all_of",
                    "rules": [
                        {
                            "attribute": "business_age_months",
                            "op": "<=",
                            "value": int(m.group(1)) * 12,
                            "confidence": self.confidence,
                        }
                    ],
                }
                return {
                    "eligibility_rules": FieldValue(
                        "eligibility_rules", "rules", rules, [s.lineage_id], self.confidence
                    )
                }
        return {}


def subscribe_eligible_user(env):
    """v1을 본 사용자: 업력 6년, 자격 규칙을 구독, 판정은 해당."""
    with env.factory() as s:
        pv = s.scalars(sa.select(models.ItemVersion)).one()
        s.add(models.Person(id="u1"))
        # 프로필 행은 동의 기록만(값은 비어 있음). 구독·판정의 외래 키가 아직 이 표를 가리킨다
        s.add(models.Profile(person_id="u1", attributes={}))
        s.flush()
        # 판정에 쓰는 값은 금고에, 판정·알림 동의와 함께(platform-services 4장)
        env.ctx.vault = vault.load_cipher()
        at = datetime(2026, 10, 1, tzinfo=UTC)
        vault.put(s, env.ctx.vault, "u1", {"business_age_months": 72}, source="input", now=at)
        for purpose in ("judge", "notify"):
            vault.grant_consent(
                s, "u1", "grant-alerts", purpose, ["business_age_months"], text_version="t", now=at
            )
        # 화면 시험용 링크. 토큰은 해시로만 남는다
        people.add_link(s, "u1", at=datetime(2026, 10, 1, tzinfo=UTC), token="u1")
        s.add(
            models.SubscriptionRow(
                app_id="grant-alerts",
                app_version=1,
                person_id="u1",
                item_id=pv.item_id,
                fields=["eligibility_rules"],
            )
        )
        s.add(
            models.JudgmentRow(
                app_id="grant-alerts",
                app_version=1,
                person_id="u1",
                item_id=pv.item_id,
                item_version_id=pv.id,
                ruleset_hash="v1",
                result="eligible",
            )
        )
        s.commit()


def test_correction_reaches_outbox_with_rejudgment(env):
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:  # v1: 자격 규칙(창업 7년)은 LLM으로, 마감은 출처 값(API 접수 기간)으로
        rows = {r.name: r for r in s.scalars(sa.select(models.FieldValueRow))}
    assert set(rows) == {"eligibility_rules", "deadline"} and rows["deadline"].value == "2026-11-07"
    assert "deadline" not in env.ctx.extractor.calls[0]  # 마감은 LLM에 묻지 않았다
    subscribe_eligible_user(env)

    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alert = s.scalars(sa.select(models.OutboxAlert)).one()
        assert alert.person_id == "u1" and alert.status == "sent"  # 알림 작업까지 돌았다
        # 결과에 앱과 판이 붙고, 그 판의 정의가 DB에 있다
        assert (alert.app_id, alert.app_version) == ("grant-alerts", 1)
        assert alert.idempotency_key.startswith("grant-alerts:u1:")
        assert s.get(models.AppDefinitionRow, ("grant-alerts", 1)) is not None
        assert {r.app_id for r in s.scalars(sa.select(models.FieldValueRow))} == {"grant-alerts"}
        assert "신청 자격 판정이 해당에서 비해당" in alert.payload["message"]
        assert (alert.payload["before"], alert.payload["after"]) == ("eligible", "ineligible")
        latest = s.scalars(
            sa.select(models.JudgmentRow).order_by(models.JudgmentRow.created_at.desc())
        ).first()
        assert latest.result == "ineligible"
    # 두 번째 추출은 바뀐 조각에 걸린 필드만 물었다. 마감은 출처 값으로 비교하므로 묻지 않는다
    first, second = env.ctx.extractor.calls
    assert second == ["eligibility_rules"] and len(second) < len(first)


def test_low_confidence_change_is_held(env):
    env.ctx.extractor = RulesFromText(confidence=0.5)
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alert = s.scalars(sa.select(models.OutboxAlert)).one()
        assert alert.status == "held" and "근거 확인" in alert.held_reason


def test_without_extractor_pipeline_stops_at_change_events(env):
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        kinds = set(s.scalars(sa.select(models.Job.kind)).all())
    assert kinds == {"load_run", "parse", "diff"}


def test_backfill_registers_versions_loaded_before_the_queue(env):
    from noticedelta.pipeline import enqueue_backfill
    from noticedelta.programs import load_snapshots

    env.collect("2026-11-07", pdf(RULES, DOCS))
    with env.factory() as s:  # 작업 큐 없이 예전 방식으로 적재
        load_snapshots(s, env.root, env.ctx.store)
        s.execute(sa.delete(models.Job))
        assert enqueue_backfill(s) == 1
        s.commit()
    env.work()
    assert env.count(models.ChangeEvent) == 1


def test_change_only_in_keys_the_app_does_not_read_carries_values_without_llm(env):
    """플랫폼 3번(app-spec 9장): 출처 단위의 판 하나를 앱마다 자기가 읽는 키로 거른다."""
    from noticedelta.grants import APP

    narrow = [
        u.model_copy(update={"read": ["pblancNm"], "attachments": False})
        if u.id == "bizinfo"
        else u
        for u in APP.sources
    ]
    env.ctx.app = APP.model_copy(update={"sources": narrow})  # 제목만 읽는 앱
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))  # 본문과 첨부만 바뀐다
    env.work()

    assert len(env.ctx.extractor.calls) == 1  # 첫 판만 뽑았다. 둘째 판에는 LLM을 부르지 않았다
    assert env.jobs().get("done", 0) and not env.jobs().get("dead")
    with env.factory() as s:
        ev = s.scalars(
            sa.select(models.ChangeEvent).where(models.ChangeEvent.from_version_id.is_not(None))
        ).one()
        c = ev.changed_lineages
        assert c["source"] == "bizinfo" and c["attachments"] is True
        assert "bsnsSumryCn" in c["keys"] and "pblancNm" not in c["keys"]
        rows = s.scalars(sa.select(models.FieldValueRow).order_by(models.FieldValueRow.name)).all()
        by_name = {}
        for r in rows:
            by_name.setdefault(r.name, []).append(r.value)
        # 판마다 두 필드. 자격은 앞 판 값을 옮겼고(이 앱은 본문을 읽지 않는다), 마감은 출처 값이
        # 바뀌어 새 값이다(읽는 키 밖에서 바뀌어도 출처 값 필드는 본다)
        assert {n: len(v) for n, v in by_name.items()} == {"deadline": 2, "eligibility_rules": 2}
        assert by_name["eligibility_rules"][0] == by_name["eligibility_rules"][1]
        assert sorted(by_name["deadline"]) == ["2026-11-07", "2026-11-14"]
        assert s.scalar(sa.select(sa.func.count()).select_from(models.OutboxAlert)) == 0
        detected = s.scalars(
            sa.select(models.EventRow).where(models.EventRow.type == "change.detected")
        ).all()
        assert any(e.payload.get("keys") for e in detected)  # 봉투에도 바뀐 키가 남는다


def test_a_fixed_past_clock_still_runs_every_job(env):
    # 작업 큐가 주입한 시계로 등록해야 과거 시각으로 다시 흘려도 작업을 꺼낸다(재생, platform-services 3.7)
    env.clock.t = datetime(2026, 10, 23, 1, tzinfo=UTC)
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    stats = env.work()
    assert (stats.done, stats.dead) == (6, 0) and env.jobs() == {"done": 6}


def test_extract_scope_spends_llm_only_on_items_in_scope(env):
    from noticedelta.pipeline import IMPACT, enqueue_item_impact

    env.ctx.extractor = RulesFromText()
    env.ctx.extract_scope = "subscribed"
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    # 구독자가 없는 사업은 LLM을 부르지 않는다(비교까지만)
    assert env.ctx.extractor.calls == [] and env.count(models.FieldValueRow) == 0
    # 구독이 생기면 그 사업의 판을 첫 판부터 차례로 계산한다
    with env.factory() as s:
        item_id = s.scalar(sa.select(models.Item.id))
        assert enqueue_item_impact(s, item_id, now=env.clock()) == 2
        assert enqueue_item_impact(s, item_id, now=env.clock()) == 0  # 다시 넣어도 새 작업은 없다
        assert s.scalar(sa.select(sa.func.count()).where(models.Job.kind == IMPACT)) == 2
        s.commit()
    env.work()
    assert len(env.ctx.extractor.calls) == 2 and not env.jobs().get("dead")


def test_changed_scope_starts_at_the_first_correction(env):
    env.ctx.extractor = RulesFromText()
    env.ctx.extract_scope = "changed"
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    assert env.ctx.extractor.calls == []  # 정정이 없는 새 사업은 아직 뽑지 않는다
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    first, second = env.ctx.extractor.calls  # 정정이 생기자 첫 판부터
    assert second == ["eligibility_rules"] and not env.jobs().get("dead")


def test_catching_up_fills_values_without_alerting_old_changes(env):
    from noticedelta.pipeline import enqueue_scope_backlog

    env.ctx.extractor = RulesFromText()
    env.ctx.extract_scope = "subscribed"
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))  # 업력 72개월은 비해당이 된다
    env.work()
    with env.factory() as s:  # 두 판이 지난 뒤에 구독한 사람
        item_id = s.scalar(sa.select(models.Item.id))
        s.add(models.Person(id="u1"))
        s.add(models.Profile(person_id="u1", attributes={}))
        s.flush()
        env.ctx.vault = vault.load_cipher()
        at = datetime(2026, 10, 1, tzinfo=UTC)
        vault.put(s, env.ctx.vault, "u1", {"business_age_months": 72}, source="input", now=at)
        vault.grant_consent(
            s, "u1", "grant-alerts", "judge", ["business_age_months"], text_version="t", now=at
        )
        s.add(models.SubscriptionRow(app_id="grant-alerts", app_version=1, person_id="u1", item_id=item_id,
                                     fields=["eligibility_rules", "deadline"]))  # fmt: skip
        assert enqueue_scope_backlog(s, env.ctx, env.clock()) == 2
        s.commit()
    env.work()
    with env.factory() as s:
        # 지난 정정(마감 연장, 자격 7년 → 5년)은 지금 구독자에게 새로 알리지 않는다. 값·판정은 채운다
        assert s.scalar(sa.select(sa.func.count()).select_from(models.OutboxAlert)) == 0
        assert s.scalar(sa.select(sa.func.count()).select_from(models.FieldValueRow)) == 4
    env.collect(
        "2026-11-21", pdf(RULES.replace("7년", "5년"), DOCS)
    )  # 구독한 뒤의 새 정정: 마감 연장
    env.work()
    with env.factory() as s:
        alerts = s.scalars(sa.select(models.OutboxAlert)).all()
        assert [(a.person_id, a.payload["field"]) for a in alerts] == [("u1", "deadline")]


def test_people_whose_values_cannot_be_read_are_not_rejudged(env):
    env.ctx.extractor = RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe_eligible_user(env)  # 판정 기록: 해당
    with env.factory() as s:  # 판정 동의를 철회했다: 금고에서 값을 꺼낼 수 없다
        vault.withdraw_consent(
            s, "u1", "grant-alerts", "judge", now=datetime(2026, 10, 2, tzinfo=UTC)
        )
        s.commit()
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        # 빈 속성으로 다시 판정해 "해당 → 확인 필요" 같은 거짓 알림을 내지 않는다
        assert s.scalar(sa.select(sa.func.count()).select_from(models.OutboxAlert)) == 0


def other_app():
    """두 번째 앱(다른 소유자가 만들었다고 본다): 지원사업 앱과 같은 출처를 읽고 같은 필드를 뽑는다."""
    from noticedelta.appspec import AppDefinition
    from noticedelta.grants import APP

    data = APP.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.update(id="rules-watch", title="(가상) 남이 만든 자격 지킴이")
    return AppDefinition.model_validate(data)


def subscribe(env, person_id, app_id, age_months):
    with env.factory() as s:
        pv = s.scalars(sa.select(models.ItemVersion)).one()
        s.add(models.Person(id=person_id))
        s.add(models.Profile(person_id=person_id, attributes={}))
        s.flush()
        at = datetime(2026, 10, 1, tzinfo=UTC)
        vault.put(s, env.ctx.vault, person_id, {"business_age_months": age_months}, source="input",
                  now=at)  # fmt: skip
        for purpose in ("judge", "notify"):
            vault.grant_consent(s, person_id, app_id, purpose, ["business_age_months"],
                                text_version="t", now=at)  # fmt: skip
        s.add(models.SubscriptionRow(app_id=app_id, app_version=1, person_id=person_id,
                                     item_id=pv.item_id, fields=["eligibility_rules"]))  # fmt: skip
        s.add(models.JudgmentRow(app_id=app_id, app_version=1, person_id=person_id,
                                 item_id=pv.item_id, item_version_id=pv.id, ruleset_hash="v1",
                                 result="eligible"))  # fmt: skip
        s.commit()


def test_another_owners_app_runs_beside_ours_without_mixing(env):
    """플랫폼 8번의 완료 기준: 다른 사람이 만든 앱이 내 앱과 섞이지 않고 돈다."""
    other = other_app()
    ours, theirs = RulesFromText(), RulesFromText()
    env.ctx.extractor, env.ctx.extra_apps = ours, (other,)
    env.ctx.extractor_for = lambda app: theirs
    env.ctx.vault = vault.load_cipher()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        apps = sorted({r.app_id for r in s.scalars(sa.select(models.FieldValueRow))})
        keys = sorted(j.dedupe_key for j in s.scalars(sa.select(models.Job)) if j.kind == "impact")
    assert apps == ["grant-alerts", "rules-watch"]  # 앱마다 자기 값을 뽑는다
    assert keys[0].startswith("impact:") and keys[1].endswith(":rules-watch")  # 우리 키는 그대로
    subscribe(env, "u1", "grant-alerts", 72)
    subscribe(env, "u2", "rules-watch", 72)

    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alerts = {a.person_id: a for a in s.scalars(sa.select(models.OutboxAlert))}
        judged = {(j.person_id, j.app_id) for j in s.scalars(sa.select(models.JudgmentRow))}
    assert set(alerts) == {"u1", "u2"}
    assert (alerts["u1"].app_id, alerts["u2"].app_id) == ("grant-alerts", "rules-watch")
    assert alerts["u2"].idempotency_key.startswith("rules-watch:u2:")
    assert all(a.status == "sent" for a in alerts.values())  # 앱마다 따로 알림 작업이 돌았다
    # 판정도 자기 앱 안에서만: u1은 우리 앱, u2는 남의 앱으로만 판정됐다
    assert ("u1", "rules-watch") not in judged and ("u2", "grant-alerts") not in judged
    assert len(ours.calls) == len(theirs.calls) == 2


def test_an_app_that_reads_other_sources_gets_no_impact_jobs(env):
    other = other_app().model_copy(update={"id": "kstartup-only"})
    data = other.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["sources"] = [u for u in data["sources"] if u["id"] == "kstartup"]
    from noticedelta.appspec import AppDefinition

    env.ctx.extractor, env.ctx.extra_apps = RulesFromText(), (AppDefinition.model_validate(data),)
    env.ctx.extractor_for = lambda app: RulesFromText()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    with env.factory() as s:
        apps = {r.app_id for r in s.scalars(sa.select(models.FieldValueRow))}
    assert apps == {"grant-alerts"}  # 기업마당 공고는 K-Startup만 읽는 앱의 일이 아니다


def test_an_app_with_only_its_own_subscribers_keeps_its_own_chain(env):
    """범위 subscribed에서 남의 앱 구독만 있는 사업: 그 앱의 앞 판을 보고 다음 판으로 간다."""
    other = other_app()
    theirs = RulesFromText()
    env.ctx.extractor, env.ctx.extra_apps = RulesFromText(), (other,)
    env.ctx.extractor_for = lambda app: theirs
    env.ctx.extract_scope = "subscribed"
    env.ctx.vault = vault.load_cipher()
    env.collect("2026-11-07", pdf(RULES, DOCS))
    env.work()
    subscribe(env, "u2", "rules-watch", 72)
    with env.factory() as s:
        from noticedelta.pipeline import enqueue_scope_backlog

        enqueue_scope_backlog(s, env.ctx.for_app("rules-watch"))
        s.commit()
    env.work()
    env.collect("2026-11-14", pdf(RULES.replace("7년", "5년"), DOCS))
    env.work()
    with env.factory() as s:
        alerts = s.scalars(sa.select(models.OutboxAlert)).all()
        apps = {r.app_id for r in s.scalars(sa.select(models.FieldValueRow))}
    assert apps == {"rules-watch"}  # 우리 앱은 구독이 없어 돌지 않았다
    assert [(a.person_id, a.app_id) for a in alerts] == [("u2", "rules-watch")]


def test_a_worker_reads_unknown_apps_from_the_registry_or_defers(env):
    from noticedelta import app_registry
    from noticedelta.jobs import Deferred

    with env.factory() as s:
        with pytest.raises(Deferred):
            env.ctx.for_app("rules-watch", s)  # 켠 적 없는 앱: 미룬다
        app_registry.register(s, other_app(), at=datetime(2026, 10, 1, tzinfo=UTC))
        app_registry.enable(s, "rules-watch", 1, at=datetime(2026, 10, 1, tzinfo=UTC))
        assert env.ctx.for_app("rules-watch", s).app.id == "rules-watch"
