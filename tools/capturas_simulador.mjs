// Rehace las capturas de la «Guia Simulador.md» (docs/img/simulador-*.png) con la pantalla actual.
//
//   node tools/capturas_simulador.mjs                 # todas
//   node tools/capturas_simulador.mjs plan detalle    # solo las que contengan esas palabras en el nombre
//
// Necesita el simulador en marcha (http://localhost:8768, tarea «Simulador web» o
// `python -m etapas.simulador_web`), Microsoft Edge y Node 22 o superior (WebSocket de serie). Abre Edge sin
// ventana por el protocolo de depuración (CDP), carga la pantalla con el buscador y la comparación lanzados,
// espera a que terminen y recorta cada panel. Ancho de 1.320 px a escala 1,42 (unos 1.770 px de imagen);
// los paneles con una tabla que no cabe se ensanchan solo lo necesario. Luego las optimiza con Pillow
// (paleta de 256 colores). No captura «Mis posiciones»: el repositorio es público y saldrían tus posiciones.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DESTINO = join(REPO, "docs", "img");
const BRUTO = join(REPO, "out", "capturas");
const PERFIL = join(REPO, "out", "edge-capturas");
const URL = "http://localhost:8768/#buscar,comparar";
const PUERTO = 9333;
const PANELES = {
  "simulador-1-controles.png": "section.panel",
  "simulador-1b-resumen.png": "#outSum",
  "simulador-1c-plan.png": "#outPlan",
  "simulador-2-abanico-precio.png": "#outFan",
  "simulador-3-resultado.png": "#outBars",
  "simulador-3b-desglose.png": "#outDes",
  "simulador-4-detalle.png": "#outTable",
  "simulador-5-buscador.png": "#outOpt",
  "simulador-6-comparar.png": "#outCmp",
  "simulador-7-recomendacion.png": "#outReco",
};
const EDGES = ["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
               "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"];

const filtro = process.argv.slice(2).map((x) => x.toLowerCase());
const elegidos = Object.entries(PANELES).filter(([f]) => !filtro.length || filtro.some((x) => f.includes(x)));
if (!elegidos.length) { console.error("Ningún panel coincide con:", filtro.join(" ")); process.exit(1); }
const edgeExe = EDGES.find(existsSync);
if (!edgeExe) { console.error("No encuentro Microsoft Edge."); process.exit(1); }
try { await fetch("http://localhost:8768/"); } catch {
  console.error("El simulador no responde en http://localhost:8768: arráncalo con la tarea «Simulador web» o "
    + "`python -m etapas.simulador_web`.");
  process.exit(1);
}
mkdirSync(BRUTO, { recursive: true });
mkdirSync(PERFIL, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const edge = spawn(edgeExe, ["--headless=new", `--remote-debugging-port=${PUERTO}`, `--user-data-dir=${PERFIL}`,
  "--no-first-run", "--hide-scrollbars", "--window-size=1320,1200", "about:blank"], { stdio: "ignore" });
const hechas = [];
try {
  let tgt;
  for (let i = 0; i < 40 && !tgt; i++) {
    await sleep(500);
    try { tgt = (await (await fetch(`http://127.0.0.1:${PUERTO}/json/list`)).json()).find((t) => t.type === "page"); } catch {}
  }
  if (!tgt) throw new Error("Edge no arrancó en modo depuración");
  const ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const pend = new Map();
  ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pend.has(d.id)) { pend.get(d.id)(d); pend.delete(d.id); } };
  const cdp = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => (await cdp("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value;
  const ancho = async (extra) => {
    await ev(`document.querySelector("main").style.maxWidth=${1280 + extra}+"px"`);
    await cdp("Emulation.setDeviceMetricsOverride", { width: 1320 + extra, height: 1200, deviceScaleFactor: 1.42, mobile: false });
    await sleep(600);
  };

  await cdp("Emulation.setDeviceMetricsOverride", { width: 1320, height: 1200, deviceScaleFactor: 1.42, mobile: false });
  await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await cdp("Page.navigate", { url: URL });
  let listo = false;
  for (let i = 0; i < 180 && !listo; i++) {
    await sleep(1000);
    listo = await ev(`(()=>{const q=s=>document.querySelector(s);
      return !q("#outReco").hidden && q("#recoTxt").textContent.trim().length>0
        && q("#optTxt").textContent.trim().length>0 && q("#cmpTab").querySelectorAll("tr").length>2
        && q("#planTab").querySelectorAll("tr").length>2})()`);
  }
  if (!listo) throw new Error("la pantalla no terminó de calcular en 3 minutos");
  await sleep(3000);

  for (const [f, sel] of elegidos) {
    await ancho(0);
    const falta = await ev(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(!e) return 0;
      return Math.max(0,...[...e.querySelectorAll(".tw")].map(t=>t.scrollWidth-t.clientWidth))})()`);
    if (falta > 0) await ancho(falta + 24);
    const r = await ev(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(!e||e.hidden) return null;
      const b=e.getBoundingClientRect(); return {x:b.left+scrollX, y:b.top+scrollY, width:b.width, height:b.height}})()`);
    if (!r) { console.log("sin panel", f); continue; }
    const s = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
      clip: { x: Math.max(0, r.x - 8), y: Math.max(0, r.y - 8), width: r.width + 16, height: r.height + 16, scale: 1 } });
    writeFileSync(join(BRUTO, f), Buffer.from(s.result.data, "base64"));
    hechas.push(f);
    console.log("ok", f, Math.round(r.width), "×", Math.round(r.height));
  }
  ws.close();
} finally {
  edge.kill();
}

// Optimiza y copia a docs/img (Pillow, como el resto del proyecto en Python).
const py = `
import sys
from pathlib import Path
from PIL import Image
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
for f in sys.argv[3:]:
    Image.open(src / f).convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(dst / f, optimize=True)
    print("  ", f, (dst / f).stat().st_size // 1024, "KB")
`;
const r = spawnSync("python", ["-c", py, BRUTO, DESTINO, ...hechas], { encoding: "utf-8" });
process.stdout.write(r.stdout || "");
if (r.status !== 0) {
  console.error("No se pudieron optimizar (¿Pillow instalado?):", r.stderr);
  console.error("Las capturas sin optimizar están en", BRUTO);
  process.exit(1);
}
console.log(`${hechas.length} capturas en docs/img. Revísalas antes de subirlas.`);
