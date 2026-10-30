# 서버 배포 (작업 목록 19번)

VM 한 대에 올린다. 부산대 피클 VM을 기준으로 쓰지만 Ubuntu VM이면 어디든 같다.

```
            인터넷
              │ 80/443
        ┌─────▼─────┐
        │   Caddy   │  컨테이너. 도메인이 있으면 HTTPS 인증서 자동
        └─────┬─────┘
              │ 127.0.0.1:8000
   ┌──────────▼──────────┐       ┌──────────────────────┐
   │ 웹 (systemd)        │──────▶│ PostgreSQL 컨테이너  │ 127.0.0.1:5432만
   └─────────────────────┘       └──────────▲───────────┘
   ┌─────────────────────┐                  │
   │ 수집+워커 (6시간마다)│──────────────────┘
   └──────────┬──────────┘
              │ docker run (네트워크 없음·읽기 전용·자원 제한)
        ┌─────▼─────┐
        │ 격리 파서 │
        └───────────┘
```

## 피클 VM 신청 사양

| 항목 | 권장 | 이유 |
|---|---|---|
| OS | Ubuntu 22.04 또는 24.04 | setup.sh가 이것을 기준으로 한다 |
| CPU / 메모리 | 2 vCPU / 4GB 이상 | PG + 웹 + 파서 컨테이너(1개당 메모리 제한 있음) |
| 디스크 | 50GB 이상 | 지금 원문 1.8GB, DB 약 1GB. 백업·도커 이미지 포함 여유 |
| 권한 | sudo | Docker 설치와 systemd 등록 |
| 네트워크 | 밖으로 나가는 HTTPS(공공데이터포털·기업마당), 들어오는 80/443 | 수집과 웹 화면 |

신청 뒤에 확인할 것:
1. **공인 IP나 학교 밖에서 접속이 되는지.** 학내망에서만 열리면 베타 사용자가 못 들어온다. 안 되면 Cloudflare Tunnel 같은 방법을 쓴다.
2. **Docker를 써도 되는지.** 격리 파서가 컨테이너라 꼭 필요하다.
3. 도메인이 있으면 DNS A 레코드를 VM IP로 맞춘다. 없으면 우선 `http://IP`로 연다.

## 처음 띄우기

```bash
sudo git clone https://github.com/03leedo/noticedelta /opt/noticedelta
cd /opt/noticedelta
sudo bash deploy/setup.sh
sudo -u noticedelta cp .env.example .env    # 그리고 아래 값을 채운다
```

`.env`에 넣을 값 (값은 채팅이나 저장소에 올리지 않는다):
- `KSTARTUP_SERVICE_KEY`, `BIZINFO_SERVICE_KEY`
- `POSTGRES_PASSWORD`: 새로 만든 긴 무작위 값
- `DATABASE_URL=postgresql+psycopg://noticedelta:<POSTGRES_PASSWORD>@127.0.0.1:5432/noticedelta`
- `SITE_ADDRESS`: 도메인(예: `notice.example.com`). 없으면 비워 둔다
- `NOTICEDELTA_METRICS_TOKEN`: `/metrics`를 볼 때 쓰는 무작위 값

```bash
sudo docker compose -f deploy/compose.yml --env-file .env up -d
sudo systemctl enable --now noticedelta-web noticedelta-collect.timer noticedelta-backup.timer
```

## 집 PC의 데이터 옮기기

원문 기록(`data/snapshots`)만 옮기고, DB는 서버에서 새로 만든다. 같은 원문을 다시 처리하면 같은 결과가 나온다.

```bash
# 집 PC에서 (Git Bash). 수집이 돌고 있지 않을 때. 약 2GB
tar czf - -C data snapshots | ssh <user>@<VM> "tar xzf - -C /tmp"
# VM에서
sudo mkdir -p /opt/noticedelta/data && sudo mv /tmp/snapshots /opt/noticedelta/data/
sudo chown -R noticedelta: /opt/noticedelta/data
sudo -u noticedelta .venv/bin/python scripts/worker.py --backfill
```

서버 수집이 돌기 시작하면 집 PC의 예약 작업 "NoticeDelta Snapshot"은 끈다. 같은 키로 두 곳에서 받으면 호출 한도를 나눠 쓰게 된다.

## 운영

| 할 일 | 명령 |
|---|---|
| 상태 | `systemctl status noticedelta-web`, `systemctl list-timers 'noticedelta*'` |
| 로그 | `journalctl -u noticedelta-collect -n 100` |
| 작업 큐 | `sudo -u noticedelta .venv/bin/python scripts/worker.py --status` |
| 지표 | `curl -H "Authorization: Bearer $TOKEN" https://<도메인>/metrics` |
| 업데이트 | `sudo -u noticedelta git pull && sudo -u noticedelta /usr/local/bin/uv sync --no-dev --extra postgres --extra web && sudo systemctl restart noticedelta-web` |
| 복원 | `docker exec -i noticedelta-postgres-1 pg_restore -U noticedelta -d noticedelta --clean < db-YYYYMMDD.dump` |

DB 마이그레이션은 스크립트가 시작할 때 저절로 올라간다(`noticedelta.config.open_db`). systemd는 `uv run` 대신 `.venv/bin/python`을 바로 부른다. `uv run`은 extras 없이 환경을 다시 맞춰 psycopg·fastapi를 지워 버리기 때문이다.

## 보안에서 정한 것

- **웹·워커를 컨테이너가 아니라 호스트에서 돌린다.** 워커는 첨부마다 격리 파서 컨테이너를 띄워야 한다. 워커를 컨테이너에 넣으면 docker 소켓을 넘겨야 하는데, 그건 호스트 root를 넘기는 것과 같다. 그래서 전용 계정 `noticedelta`를 docker 그룹에 넣고 호스트에서 돌린다. 이것도 root에 가까운 권한이라, 이 VM에는 다른 서비스를 같이 올리지 않는다.
- 파서는 지금처럼 네트워크 없음·읽기 전용·메모리 제한·비루트로 돈다. 첨부에 악성 파일이 있어도 파서 컨테이너 밖으로 못 나온다.
- 밖에 여는 포트는 Caddy의 80/443과 SSH뿐이다. PG는 127.0.0.1에만 붙고, 라벨 화면(8001)은 서버에 띄우지 않는다.
- 원문은 서버 디스크에만 있고 웹에는 원문을 길게 싣지 않는다(이용 조건).
- 백업은 매일 04:30 DB 덤프, 14일 보관(`/var/backups/noticedelta`). 원문은 크고 다시 받을 수 없는 과거 판이 있으니 가끔 같은 tar 방법으로 집 PC에 받아 둔다.
