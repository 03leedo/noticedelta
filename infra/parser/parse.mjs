// kordoc으로 파싱하고, HWP·HWPX에서 실패하면 hwp-hwpx-parser로 한 번 더 시도한다.
// 출력 계약(contract 1)은 noticedelta/parsing.py의 ParsedDocument와 같다.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

const CONTRACT = 1;
const KORDOC_VERSION = "4.18.13";
const HWP_MIMES = new Set(["application/x-hwp", "application/hwp+zip"]);
const mime = process.argv[2] || "";
const input = readFileSync(0);

function emit(doc) {
  process.stdout.write(`\n@@PARSED@@${JSON.stringify({ contract: CONTRACT, ...doc })}\n`);
}

// 병합 칸이 덮는 자리는 kordoc이 빈 칸으로 채운다. 덮인 자리를 null로 표시한다.
function grid(table) {
  const rows = table.cells.map((row) => row.map((c) => (c.text ?? "").replace(/!\[image\]/g, "").trim()));
  const covered = table.cells.map((row) => row.map(() => false));
  table.cells.forEach((row, r) =>
    row.forEach((c, k) => {
      for (let dr = 0; dr < (c.rowSpan || 1); dr++)
        for (let dc = 0; dc < (c.colSpan || 1); dc++)
          if ((dr || dc) && covered[r + dr] && covered[r + dr][k + dc] !== undefined) covered[r + dr][k + dc] = true;
    }),
  );
  return rows.map((row, r) => row.map((t, k) => (covered[r][k] && t === "" ? null : t)));
}

function flatten(blocks, out) {
  for (const b of blocks) {
    const page = b.pageNumber ?? null;
    if (b.type === "heading" || b.type === "paragraph") {
      const text = (b.text ?? "").trim();
      if (text) out.push({ type: b.type, text, level: b.level ?? null, page });
    } else if (b.type === "list") {
      if (b.text?.trim()) out.push({ type: "paragraph", text: b.text.trim(), level: null, page });
      flatten(b.children ?? [], out);
    } else if (b.type === "table" && b.table) {
      const rows = grid(b.table);
      if (rows.length === 1 && rows[0].length === 1) {
        // 칸 하나짜리 표는 장식 상자다. 문단으로 본다.
        if (rows[0][0]) out.push({ type: "paragraph", text: rows[0][0], level: null, page });
      } else out.push({ type: "table", rows, page });
    }
  }
  return out;
}

let doc;
try {
  const { parse } = await import("kordoc");
  const r = await parse(input, { ocr: false });
  if (r.success) {
    doc = { status: "ok", parser: "kordoc", parser_version: KORDOC_VERSION, blocks: flatten(r.blocks ?? [], []),
            warnings: (r.warnings ?? []).length, error: null };
  } else {
    doc = { status: "failed", parser: "kordoc", parser_version: KORDOC_VERSION, blocks: [], warnings: 0,
            error: `${r.code ?? ""} ${String(r.error).slice(0, 300)}`.trim() };
  }
} catch (e) {
  doc = { status: "failed", parser: "kordoc", parser_version: KORDOC_VERSION, blocks: [], warnings: 0,
          error: `${e.name}: ${String(e.message).slice(0, 300)}` };
}

if (doc.status !== "ok" && HWP_MIMES.has(mime)) {
  const p = spawnSync("python3", ["-I", "/parser/fallback.py", mime], { input, encoding: "utf8", timeout: 90000,
                                                                       maxBuffer: 64 * 1024 * 1024 });
  const i = (p.stdout || "").lastIndexOf("@@PARSED@@");
  if (i >= 0) {
    const fb = JSON.parse(p.stdout.slice(i + 10).split("\n")[0]);
    if (fb.status === "ok") doc = { ...fb, error: `kordoc 실패 후 대체: ${doc.error}` };
  }
}
emit(doc);
