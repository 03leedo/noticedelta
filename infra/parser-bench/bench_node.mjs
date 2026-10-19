// kordoc 실행기. 파일마다 자식 프로세스로 파싱해 시간 제한을 건다.
// 출력: /out/kordoc/<key>.txt (Markdown), stdout에 결과 JSON 한 줄.
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const TIMEOUT_MS = 60000;
const MIMES = new Set(["application/x-hwp", "application/hwp+zip", "application/pdf"]);

if (process.argv[2] === "one") {
  const { parse } = await import("kordoc");
  const key = process.argv[3];
  const buf = readFileSync(`/in/blobs/${key}`);
  const t0 = performance.now();
  let res;
  try {
    const r = await parse(buf);
    if (r.success) {
      const tables = (r.blocks || []).filter((b) => b.type === "table");
      const cells = tables.reduce((n, b) => n + (b.table?.cells || []).reduce((m, row) => m + row.length, 0), 0);
      mkdirSync("/out/kordoc", { recursive: true });
      writeFileSync(`/out/kordoc/${key}.txt`, r.markdown || "");
      res = { ok: true, tables: tables.length, cells, warnings: (r.warnings || []).length };
    } else {
      res = { ok: false, error: `${r.code || ""} ${String(r.error).slice(0, 200)}`.trim() };
    }
  } catch (e) {
    res = { ok: false, error: `${e.name}: ${String(e.message).slice(0, 200)}` };
  }
  res.ms = Math.round(performance.now() - t0);
  // pdfjs가 표준 출력에 경고를 쓰므로 표시 뒤에 결과를 둔다.
  process.stdout.write(`\n@@RESULT@@${JSON.stringify(res)}\n`);
} else {
  const manifest = JSON.parse(readFileSync("/in/manifest.json", "utf8"));
  for (const item of manifest) {
    if (!MIMES.has(item.mime)) continue;
    const p = spawnSync(process.execPath, [process.argv[1], "one", item.key], {
      timeout: TIMEOUT_MS,
      maxBuffer: 16 * 1024 * 1024,
      encoding: "utf8",
    });
    let res;
    if (p.error?.code === "ETIMEDOUT" || p.signal) res = { ok: false, error: "timeout" };
    else {
      const i = (p.stdout || "").lastIndexOf("@@RESULT@@");
      try { res = JSON.parse(p.stdout.slice(i + 10).split("\n")[0]); }
      catch { res = { ok: false, error: `exit ${p.status}: ${(p.stderr || p.stdout || "").slice(-200)}` }; }
    }
    console.log(JSON.stringify({ key: item.key, parser: "kordoc", ...res }));
  }
}
