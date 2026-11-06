/* repack_asar.js — пересобирает app.asar OpenCode Desktop с квотным виджетом.
 * Использование: node repack_asar.js <src.asar> <dst.asar>
 *
 * Что делает:
 *  - в out/renderer/index.html вставляет <script src="./oc-quota-widget.js"></script>
 *    (идемпотентно: если тег уже есть — не дублирует);
 *  - добавляет файл out/renderer/oc-quota-widget.js (берётся из папки этого скрипта);
 *  - всё остальное — побайтово идентично исходнику.
 *
 * Формат сохраняется 1:1 как у исходного asar этого приложения:
 * pickle-заголовок [4 | 8+align4(len) | 4+align4(len) | len], JSON-дерево,
 * десятичные строковые offset, integrity SHA256 с blockSize 4194304.
 *
 * После сборки скрипт сам себя проверяет: перечитывает dst, сверяет все
 * integrity-хэши и diff против src (ожидаются ровно 2 изменения).
 */
"use strict";
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const SRC = process.argv[2];
const DST = process.argv[3];
if (!SRC || !DST) { console.error("usage: node repack_asar.js <src.asar> <dst.asar>"); process.exit(1); }

const WIDGET_REL = "out/renderer/oc-quota-widget.js";
const HTML_REL = "out/renderer/index.html";
const SCRIPT_TAG = '<script src="./oc-quota-widget.js"></script>';
const BLOCK = 4194304;
const sha256hex = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

function integrityOf(buf) {
  const blocks = [];
  for (let o = 0; o < buf.length; o += BLOCK)
    blocks.push(sha256hex(buf.subarray(o, Math.min(o + BLOCK, buf.length))));
  if (blocks.length === 0) blocks.push(sha256hex(Buffer.alloc(0)));
  return { algorithm: "SHA256", hash: sha256hex(buf), blockSize: BLOCK, blocks };
}

// ---------- read source ----------
const b = fs.readFileSync(SRC);
const jsonLen = b.readUInt32LE(12);
if (jsonLen < 100 || 16 + jsonLen > b.length) { console.error("bad asar: jsonLen=" + jsonLen); process.exit(1); }
const header = JSON.parse(b.toString("utf8", 16, 16 + jsonLen));
const base = 16 + ((jsonLen + 3) & ~3);

const files = []; // {rel, entry, buf|null(unpacked)} in original DFS/data order
(function walk(node, dir) {
  for (const name of Object.keys(node.files || {})) {
    const f = node.files[name];
    const rel = dir ? dir + "/" + name : name;
    if (f.files) walk(f, rel);
    else {
      if (f.unpacked) { files.push({ rel, entry: f, buf: null }); continue; }
      const off = parseInt(f.offset, 10);
      if (isNaN(off) || off < 0) { console.error("bad offset for " + rel + ": " + JSON.stringify(f.offset)); process.exit(1); }
      const end = base + off + f.size;
      if (end > b.length) { console.error("offset out of range: " + rel); process.exit(1); }
      const buf = b.subarray(base + off, end);
      if (buf.length !== f.size) { console.error("size mismatch: " + rel); process.exit(1); }
      files.push({ rel, entry: f, buf });
    }
  }
})(header, "");
console.log("entries: " + files.length + ", data bytes: " + (b.length - base));

// ---------- modifications ----------
const widgetBuf = fs.readFileSync(path.join(__dirname, "oc-quota-widget.js"));

let htmlBuf = null;
for (const f of files) if (f.rel === HTML_REL) htmlBuf = f.buf;
if (!htmlBuf) { console.error("index.html not found in asar"); process.exit(1); }
let htmlChanged = false;
let html = htmlBuf.toString("utf8");
if (!html.includes("oc-quota-widget.js")) {
  if (!html.includes("</body>")) { console.error("no </body> in index.html — aborting"); process.exit(1); }
  html = html.replace("</body>", SCRIPT_TAG + "\n  </body>");
  htmlChanged = true;
}
const newHtmlBuf = Buffer.from(html, "utf8");

// insert widget entry into header tree (end of out/renderer dir => end of data)
const rendDir = header.files.out.files.renderer.files;
rendDir[WIDGET_REL.split("/").pop()] = {
  size: widgetBuf.length,
  offset: "0", // заполним ниже
  integrity: integrityOf(widgetBuf),
};

// ---------- layout data (same DFS order, widget appended last) ----------
const bufs = [];
let off = 0;
for (const f of files) {
  if (f.buf === null) continue; // unpacked: нет данных в asar
  let buf = f.buf;
  if (f.rel === HTML_REL) { buf = newHtmlBuf; f.entry.size = buf.length; f.entry.integrity = integrityOf(buf); }
  f.entry.offset = String(off);
  bufs.push(buf);
  off += buf.length;
}
rendDir["oc-quota-widget.js"].offset = String(off);
bufs.push(widgetBuf);
const totalData = off + widgetBuf.length;

// ---------- serialize ----------
const newJson = Buffer.from(JSON.stringify(header), "utf8");
const align = (n) => (n + 3) & ~3;
const head = Buffer.alloc(16 + align(newJson.length));
head.writeUInt32LE(4, 0);
head.writeUInt32LE(8 + align(newJson.length), 4);
head.writeUInt32LE(4 + align(newJson.length), 8);
head.writeUInt32LE(newJson.length, 12);
newJson.copy(head, 16);
const out = Buffer.concat([head, ...bufs]);

const tmp = DST + ".tmp";
fs.writeFileSync(tmp, out);
if (fs.existsSync(DST)) fs.unlinkSync(DST);
fs.renameSync(tmp, DST);
console.log("written: " + DST + " (" + out.length + " bytes, data=" + totalData + ")");
console.log("index.html changed: " + htmlChanged + ", widget: " + widgetBuf.length + " bytes");

// ---------- verify ----------
const v = fs.readFileSync(DST);
const vjLen = v.readUInt32LE(12);
const vh = JSON.parse(v.toString("utf8", 16, 16 + vjLen));
const vbase = 16 + ((vjLen + 3) & ~3);
let bad = 0, checked = 0, diff = 0;
const srcByRel = new Map(files.map((f) => [f.rel, f]));
(function vwalk(node, dir) {
  for (const name of Object.keys(node.files || {})) {
    const f = node.files[name];
    const rel = dir ? dir + "/" + name : name;
    if (f.files) { vwalk(f, rel); continue; }
    if (f.unpacked) continue;
    const off2 = parseInt(f.offset, 10);
    const buf = v.subarray(vbase + off2, vbase + off2 + f.size);
    checked++;
    if (f.integrity && f.integrity.hash && sha256hex(buf) !== f.integrity.hash) {
      console.error("HASH MISMATCH: " + rel); bad++; continue;
    }
    if (rel === WIDGET_REL) {
      if (!buf.equals(widgetBuf)) { console.error("WIDGET CONTENT MISMATCH"); bad++; }
      continue;
    }
    const old = srcByRel.get(rel);
    if (!old || old.buf === null) { console.error("UNEXPECTED ENTRY: " + rel); diff++; continue; }
    if (old.rel === HTML_REL && htmlChanged) { diff++; } // ожидаемое: index.html
    else if (!old.buf.equals(buf)) { console.error("UNEXPECTED DIFF: " + rel); diff++; }
  }
})(vh, "");
const expectedDiff = htmlChanged ? 1 : 0;
console.log("verify: checked=" + checked + " hashBad=" + bad + " diffsFromSrc=" + diff + " (ожидается " + expectedDiff + ")");
if (bad > 0 || diff !== expectedDiff) { console.error("VERIFICATION FAILED"); process.exit(1); }
console.log("OK");
