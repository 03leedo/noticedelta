# 격리 파서 (설계 7.3, docs/research/parsers.md).
# 입력: 표준 입력으로 파일 바이트, 인자로 MIME. 출력: 표준 출력에 "@@PARSED@@" 뒤 JSON 한 줄.
# 실행은 네트워크 없음·읽기 전용·자원 제한·비루트로 한다 (noticedelta.parsing.DockerParser).
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
 && pip3 install --no-cache-dir --break-system-packages hwp-hwpx-parser==1.0.0 \
 && apt-get purge -y python3-pip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /parser
RUN npm init -y >/dev/null \
 && npm install --omit=optional --no-audit --no-fund kordoc@4.18.13 pdfjs-dist@4.10.38
COPY parse.mjs fallback.py ./
USER node
ENTRYPOINT ["node", "/parser/parse.mjs"]
