"""Vigila la calibración del simulador de pools y avisa por correo si se desvía.

    python -m etapas.simulador_aviso              # calibra, guarda el resultado y abre/cierra la issue
    python -m etapas.simulador_aviso --simular    # solo dice qué haría, sin tocar GitHub

Lanza `simulador.calibrar` con las aperturas del último año a cuatro plazos (7, 30, 90 y 180 días,
rango ±10 %, con el histórico que el simulador usa para cada plazo) en los pools de referencia (TVL
> 2 M$, histórico suficiente para el plazo, sin los de datos anómalos ni pares de dos estables) y
junta los resultados por plazo:
  - mediana entre pools de comisiones reales / media simulada, fuera de su banda → el
    `AJUSTE_COMISIONES` ya no vale (el de 30 días es el que lo fija; los demás avisan de que las
    comisiones se desvían a ese plazo);
  - lo real dentro del P10–P90: fuera de su banda → la horquilla es demasiado estrecha o ancha.
Si algún plazo se desvía, abre una issue de GitHub (etiqueta «simulador-calibracion», que GitHub
envía por correo) con lo que hay que mirar; si todos vuelven a estar en banda, la cierra. Con la
issue abierta no repite el aviso. Cada ejecución se añade a out/simulador_calibracion.jsonl.

Necesita MetaProDB (D:), así que no va en la rutina de GitHub Actions: lo lanza la tarea de Windows
«Simulador calibración» una vez al mes (simulador_aviso.vbs → .bat, como el aviso de Aave).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import simulador as S

# Datos que el simulador no reproduce (fee_growth anómalo en MetaProDB): fuera de la referencia.
ANOMALOS = {("ethereum", "0x8a7e585048bda875e64024118c506b14f78166dd"),     # PAXG/WETH 0,3 %
            ("arbitrum", "0x5969efdde3cf5c0d9a88ae51e47d721096a97203"),     # WBTC/USDT 0,05 %
            ("ethereum", "0x109830a1aaad605bbf02a9dfa7b0b92ec2fb7daa"),     # wstETH/WETH 0,01 %
            ("arbitrum", "0x0e4831319a50228b9e450861297ab92dee15b44f")}     # USDC/WBTC 0,05 %
ANCHO, ULTIMOS, MIN_TVL = 0.10, 365, 2e6
PRINCIPAL = 30                       # el plazo que fija AJUSTE_COMISIONES
# plazo → (comisiones mín, máx, P10–P90 mín, máx, días entre aperturas). A 90 y 180 días las aperturas
# de un año se solapan mucho (pocas pruebas independientes): bandas algo más anchas. Una banda de
# comisiones None las deja solo registradas (sin avisar).
BANDAS = {7: (0.85, 1.15, 0.70, 0.90, 7), 30: (0.90, 1.10, 0.70, 0.90, 7),
          90: (0.80, 1.20, 0.65, 0.92, 14), 180: (0.80, 1.20, 0.65, 0.92, 14)}
PLAZOS = tuple(BANDAS)
LABEL = "simulador-calibracion"
MARKER = f"<!-- {LABEL} -->"
REPO = os.environ.get("GITHUB_REPOSITORY", "Caiico1/etapas-weinstein")
LOG = S.CALIBRACION_LOG               # la pantalla lee de aquí la calibración de cada pool


def historia(dias: int) -> int:
    return S.historia_para(dias)


def min_dias(dias: int) -> int:
    """Histórico que necesita un pool para entrar en la referencia de ese plazo."""
    return historia(dias) + ULTIMOS + dias + 30


@dataclass
class Verdict:
    dias: int                 # plazo
    ratio: float              # mediana entre pools de comisiones reales / media simulada
    cobertura: float          # fracción con lo real dentro del P10–P90
    pruebas: int
    ajuste: float             # el que se usó
    por_pool: list[dict]

    @property
    def bandas(self) -> tuple:
        return BANDAS.get(self.dias, BANDAS[PRINCIPAL])

    @property
    def ok_comisiones(self) -> bool:
        lo, hi = self.bandas[:2]
        return lo is None or lo <= self.ratio <= hi

    @property
    def ok_cobertura(self) -> bool:
        lo, hi = self.bandas[2:4]
        return lo <= self.cobertura <= hi

    @property
    def ok(self) -> bool:
        return self.ok_comisiones and self.ok_cobertura

    @property
    def ajuste_propuesto(self) -> float:
        return round(self.ajuste * self.ratio, 2)


def combine(results: list[dict], ajuste: float, dias: int = PRINCIPAL) -> Verdict:
    rows = [r for c in results for r in c["detalle"]]
    if not rows:
        raise ValueError(f"la calibración a {dias} días no produjo ninguna prueba")
    ratios = [c["comisiones_real_vs_p50"] for c in results]
    inside = sum(0.1 <= r["pct"] <= 0.9 for r in rows)
    per = [{"pool": c["pool"], "red": c["red"], "pool_id": c.get("pool_id"), "pruebas": c["pruebas"],
            "comisiones": round(c["comisiones_real_vs_p50"], 3), "cobertura": round(c["dentro_p10_p90"], 3)}
           for c in results]
    return Verdict(dias, float(pd.Series(ratios).median()), inside / len(rows), len(rows), ajuste, per)


def reference_pools(min_dias: int = min_dias(PRINCIPAL)) -> list[tuple[str, str]]:
    """(pool_id, red) con al menos `min_dias` de histórico y TVL actual > MIN_TVL. Sin pares de dos
    estables: su valor final apenas se mueve y la horquilla P10–P90 no mide nada."""
    from . import backtest as B
    con = B._connect()
    try:
        rows = con.execute("""SELECT pool_id, chain, any_value(symbol0), any_value(symbol1) FROM pool_hour_v
                              GROUP BY pool_id, chain
                              HAVING max(hour) - min(hour) >= to_days(?) AND arg_max(tvl_usd, hour) > ?
                              ORDER BY arg_max(tvl_usd, hour) DESC""", [min_dias, MIN_TVL]).fetchall()
    finally:
        con.close()
    return [(p, c) for p, c, s0, s1 in rows
            if (c, p) not in ANOMALOS and not (s0 in B._STABLE and s1 in B._STABLE)]


def _calibrar(args: tuple) -> dict | None:
    key, chain, dias, ajuste = args
    try:
        return S.calibrar(key, chain, dias, ANCHO, historia(dias), ajuste=ajuste, ultimos=ULTIMOS,
                          paso=BANDAS[dias][4])
    except Exception as e:                                       # noqa: BLE001 (un pool no tumba el resto)
        print(f"{key} {chain} {dias} d: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def measure(ajuste: float = S.AJUSTE_COMISIONES, plazos: tuple[int, ...] = PLAZOS,
            pools: dict[int, list[tuple[str, str]]] | None = None, procesos: int = 4) -> list[Verdict]:
    pools = pools or {d: reference_pools(min_dias(d)) for d in plazos}
    jobs = [(k, c, d, ajuste) for d in plazos for k, c in pools[d]]
    with ProcessPoolExecutor(procesos) as ex:
        res = list(ex.map(_calibrar, jobs))
    out = []
    for d in plazos:
        rs = [r for j, r in zip(jobs, res) if j[2] == d and r]
        out.append(combine(rs, ajuste, d))
    return out


def _bandas_txt(v: Verdict) -> tuple[str, str]:
    a, b, c, d, _ = v.bandas
    return (f"{a:.2f}–{b:.2f}" if a is not None else "informativo"), f"{c:.0%}–{d:.0%}"


def title(vs: list[Verdict]) -> str:
    what = []
    for v in vs:
        if not v.ok_comisiones:
            what.append(f"{v.dias} d comisiones {v.ratio:.2f}")
        if not v.ok_cobertura:
            what.append(f"{v.dias} d P10–P90 {v.cobertura:.0%}")
    return "Simulador de pools: recalibrar (" + ", ".join(what) + ")"


def body(vs: list[Verdict], hoy: str) -> str:
    lines = [f"Calibración del {hoy}: aperturas del último año, rango ±{ANCHO:.0%}, a {len(vs)} plazos.", "",
             "| Plazo | Pools | Pruebas | Comisiones real/simulado | Banda | Real dentro del P10–P90 | Banda | |",
             "|---:|---:|---:|---:|---|---:|---|---|"]
    for v in vs:
        bc, bp = _bandas_txt(v)
        lines.append(f"| {v.dias} d | {len(v.por_pool)} | {v.pruebas} | {v.ratio:.2f} | {bc} | {v.cobertura:.0%} | {bp} | "
                     + ("✅" if v.ok else "⚠️") + " |")
    lines += [""]
    for v in vs:
        if v.ok:
            continue
        lines += [f"### Plazo de {v.dias} días", "", "| Pool | Red | Pruebas | Comisiones | P10–P90 |",
                  "|---|---|---:|---:|---:|"]
        lines += [f"| {p['pool']} | {p['red']} | {p['pruebas']} | {p['comisiones']:.2f} | {p['cobertura']:.0%} |"
                  for p in v.por_pool]
        lines += [""]
        if not v.ok_comisiones:
            if v.dias == PRINCIPAL:
                lines += [f"**Qué hacer:** cambiar `AJUSTE_COMISIONES` en `etapas/simulador.py` de {v.ajuste:.2f} a "
                          f"**{v.ajuste_propuesto:.2f}** (o probarlo antes en la pantalla, campo «Ajuste comisiones») y "
                          "reiniciar `python -m etapas.simulador_web`.", ""]
            else:
                lines += [f"**Qué hacer:** a {v.dias} días las comisiones simuladas se desvían (real/simulado "
                          f"{v.ratio:.2f}). No toques el ajuste común (lo fija el plazo de {PRINCIPAL} días): revisa "
                          f"`EXPONENTE_PLAZO` ({S.EXPONENTE_PLAZO:.2f}) en `etapas/simulador.py`, que corrige las comisiones "
                          "por plazo; si a un plazo largo salen altas, súbelo (y si salen bajas, bájalo), y compruébalo "
                          f"con `--calibrar --dias {v.dias} --exponente …`.", ""]
        if not v.ok_cobertura:
            corta = v.cobertura < v.bandas[2]
            lines += [f"**Qué hacer:** a {v.dias} días la horquilla P10–P90 "
                      + (f"se queda corta: lo real se sale más de lo previsto. Prueba más histórico (`--historia "
                         f"{historia(v.dias) + 365}`)." if corta else
                         f"es demasiado ancha: prueba menos histórico (`--historia {max(365, historia(v.dias) - 365)}`).")
                      + f" Compruébalo con `python -m etapas.simulador \"WETH/USDC 0.05%\" --red ethereum --calibrar "
                        f"--dias {v.dias} --ultimos {ULTIMOS}`.", ""]
    lines += ["Esta issue se cierra sola en la siguiente calibración que esté en banda.", "", MARKER]
    return "\n".join(lines)


class GitHub:
    def __init__(self, repo: str = REPO):
        self.repo = repo

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args, "--repo", self.repo], check=True, capture_output=True,
                              text=True, encoding="utf-8").stdout

    def open_issue(self) -> int | None:
        out = json.loads(self._gh("issue", "list", "--label", LABEL, "--state", "open",
                                  "--json", "number,body", "--limit", "20"))
        return next((i["number"] for i in out if MARKER in (i.get("body") or "")), None)

    def create(self, title: str, body: str) -> None:
        self._gh("label", "create", LABEL, "--color", "6f7cff", "--force",
                 "--description", "El simulador de pools necesita recalibrarse")
        self._gh("issue", "create", "--title", title, "--body", body, "--label", LABEL,
                 "--assignee", self.repo.split("/")[0])

    def close(self, n: int, comment: str) -> None:
        self._gh("issue", "close", str(n), "--comment", comment)


# Tus propias simulaciones (simulador_historial): con al menos HIST_MIN vencidas, lo real debe caer dentro
# del P10–P90 entre HIST_BANDA. En la issue (repositorio público) solo van el número y el porcentaje.
HIST_MIN, HIST_BANDA = 20, (0.65, 0.92)


def historial() -> dict | None:
    """Evalúa las simulaciones guardadas que han vencido y devuelve su resumen (None si falla)."""
    from . import simulador_historial as H
    try:
        H.evaluar(maximo=500)
        return H.resumen()
    except Exception as e:                                       # noqa: BLE001 (complemento)
        print(f"historial: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def historial_ok(h: dict | None) -> bool:
    if not h or h.get("evaluadas", 0) < HIST_MIN:
        return True
    return HIST_BANDA[0] <= h["dentro_p10_p90"] <= HIST_BANDA[1]


def _hist_txt(h: dict | None) -> str:
    if not h or not h.get("evaluadas"):
        return ""
    return (f"tus simulaciones: {h['evaluadas']} vencidas, P10–P90 {h['dentro_p10_p90']:.0%}"
            + ("" if historial_ok(h) else " ⚠"))


def decide(vs: list[Verdict], open_n: int | None, hist_ok: bool = True) -> str | None:
    """abrir | cerrar | None."""
    ok = all(v.ok for v in vs) and hist_ok
    if not ok and open_n is None:
        return "abrir"
    if ok and open_n is not None:
        return "cerrar"
    return None


def _corto(vs: list[Verdict]) -> str:
    return " · ".join(f"{v.dias} d: comisiones {v.ratio:.2f}, P10–P90 {v.cobertura:.0%}" + ("" if v.ok else " ⚠")
                      for v in vs)


def run(gh: GitHub | None, simulate: bool = False, vs: list[Verdict] | None = None,
        hist: dict | None = None) -> str:
    vs = vs or measure()
    hist = hist if hist is not None else historial()
    hok = historial_ok(hist)
    hoy = pd.Timestamp.now().strftime("%Y-%m-%d")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"fecha": hoy, "ok": all(v.ok for v in vs), "ajuste": vs[0].ajuste,
                            "plazos": [{"dias": v.dias, "ratio": round(v.ratio, 4), "cobertura": round(v.cobertura, 4),
                                        "pruebas": v.pruebas, "ok": v.ok, "pools": v.por_pool} for v in vs]},
                           ensure_ascii=False) + "\n")
    principal = next((v for v in vs if v.dias == PRINCIPAL), None)
    ok = all(v.ok for v in vs) and hok
    ht = _hist_txt(hist)
    summary = f"{hoy}: {_corto(vs)}{' · ' + ht if ht else ''} → " + ("en banda" if ok else "DESVIADO" + (
        f" (ajuste propuesto {principal.ajuste_propuesto:.2f})" if principal and not principal.ok_comisiones else ""))
    open_n = gh.open_issue() if gh else None
    act = decide(vs, open_n, hok)
    if act and not simulate and gh:
        if act == "abrir":
            b = body(vs, hoy)
            if not hok:
                b = b.replace(MARKER, f"### Tus simulaciones\n\nDe {hist['evaluadas']} simulaciones guardadas en la "
                              f"pantalla que ya han vencido, lo real cayó dentro del P10–P90 el {hist['dentro_p10_p90']:.0%} "
                              f"(banda {HIST_BANDA[0]:.0%}–{HIST_BANDA[1]:.0%}). Detalle local: "
                              "`python -m etapas.simulador_historial`.\n\n" + MARKER)
            gh.create(title(vs) if not all(v.ok for v in vs) else "Simulador de pools: tus simulaciones se desvían", b)
        else:
            gh.close(open_n, f"Calibración del {hoy} en banda: {_corto(vs)}. Se cierra el aviso.")
    return summary + (f" · {act} issue" if act else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simular", action="store_true", help="no tocar GitHub")
    a = ap.parse_args(argv)
    try:
        print(run(None if a.simular else GitHub(), a.simular))
    except subprocess.CalledProcessError as e:
        print(f"gh falló: {e.stderr}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
