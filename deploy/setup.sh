#!/usr/bin/env bash
# Ubuntu 22.04/24.04 VM 첫 설치. root로 한 번 돌린다:
#   sudo bash deploy/setup.sh            (저장소를 /opt/noticedelta에 받은 뒤)
# 다시 돌려도 된다. 끝나면 .env를 채우고 docs/deploy.md의 "처음 띄우기"를 따른다.
set -euo pipefail
APP=/opt/noticedelta
cd "$APP"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

# 서비스 계정. 워커가 격리 파서 컨테이너를 띄우려면 docker 그룹이 필요하다(사실상 root 권한, docs/deploy.md).
id noticedelta >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin noticedelta
usermod -aG docker noticedelta
chown -R noticedelta:noticedelta "$APP"
chmod +x deploy/backup.sh

sudo -u noticedelta /usr/local/bin/uv sync --no-dev --extra postgres --extra web
docker build -t noticedelta-parser infra/parser

cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
echo "설치 끝. .env를 채운 뒤 docs/deploy.md의 '처음 띄우기'를 따르세요."
