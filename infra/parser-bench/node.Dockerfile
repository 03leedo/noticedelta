# 파서 비교용 kordoc 이미지. 버전은 공개 2주 이상 지난 것으로 고정한다.
FROM node:22-slim
WORKDIR /bench
RUN npm init -y >/dev/null \
 && npm install --omit=optional --no-audit --no-fund kordoc@4.18.13 pdfjs-dist@4.10.38
COPY bench_node.mjs /bench/bench_node.mjs
USER node
ENTRYPOINT ["node", "/bench/bench_node.mjs"]
