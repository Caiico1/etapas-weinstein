"""Vigila la calibración del simulador de pools y avisa por correo si se desvía.

    python -m etapas.simulador_aviso              # calibra, guarda el resultado y abre/cierra la issue
    python -m etapas.simulador_aviso --simular    # solo dice qué haría, sin tocar GitHub

Lanza `simulador.calibrar` con las aperturas del último año (30 días, rango ±10 %, histórico de
730 días) en los pools de referencia (≥ 3 años de datos y TVL > 2 M$, sin los de datos anómalos)
y junta los resultados:
  - mediana entre pools de comisiones reales / media simulada: fuera de 0,90–1,10 → el
    `AJUSTE_COMISIONES` ya no vale;
  - lo real dentro del P10–P90: fuera del 70–90 % → la horquilla es demasiado estrecha o ancha.
Si se desvía, abre una issue de GitHub (etiqueta «simulador-calibracion», que GitHub envía por
correo) con el ajuste propuesto; si vuelve a estar en banda, la cierra. Con la issue abierta no
repite el aviso. Cada ejecución se añade a out/simulador_calibracion.jsonl.

Necesita MetaProDB (D:), así que no va en la rutina de GitHub Actions: lo lanza la tarea de Windows
«Simulador calibración» una vez al mes (simulador_aviso.vbs → .bat, como el aviso de Aave).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import simulador as S

# Datos que el simulador no reproduce (fee_growth anómalo en MetaProDB): fuera de la referencia.
ANOMALOS = {("ethereum", "0x8a7e585048bda875e64024118c506b14f78166dd"),     # PAXG/WETH 0,3 %
            ("arbitrum", "0x5969efdde3cf5c0d9a88ae51e47d721096a97203"),     # WBTC/USDT 0,05 %
            ("ethereum", "0x109830a1aaad605bbf02a9dfa7b0b92ec2fb7daa"),     # wstETH/WETH 0,01 %
            ("arbitrum", "0x0e4831319a50228b9e450861297ab92dee15b44f")}     # USDC/WBTC 0,05 %
DIAS, ANCHO, HISTORIA, ULTIMOS = 30, 0.10, S.HISTORIA_DIAS, 365
MIN_TVL, MIN_DIAS = 2e6, HISTORIA + ULTIMOS + 30
COMIS_MIN, COMIS_MAX = 0.90, 1.10
COBER_MIN, COBER_MAX = 0.70, 0.90
LABEL = "simulador-calibracion"
MARKER = f"<!-- {LABEL} -->"
REPO = os.environ.get("GITHUB_REPOSITORY", "Caiico1/etapas-weinstein")
LOG = Path(__file__).resolve().parent.parent / "out" / "simulador_calibracion.jsonl"


@dataclass
class Verdict:
    ratio: float              # mediana entre pools de comisiones reales / media simulada
    cobertura: float          # fracción con lo real dentro del P10–P90
    pruebas: int
    ajuste: float             # el que se usó
    por_pool: list[dict]

    @property
    def ok_comisiones(self) -> bool:
        return COMIS_MIN <= self.ratio <= COMIS_MAX

    @property
    def ok_cobertura(self) -> bool:
        return COBER_MIN <= self.cobertura <= COBER_MAX

    @property
    def ok(self) -> bool:
        return self.ok_comisiones and self.ok_cobertura

    @property
    def ajuste_propuesto(self) -> float:
        return round(self.ajuste * self.ratio, 2)


def combine(results: list[dict], ajuste: float) -> Verdict:
    rows = [r for c in results for r in c["detalle"]]
    if not rows:
        raise ValueError("la calibración no produjo ninguna prueba")
    ratios = [c["comisiones_real_vs_p50"] for c in results]
    inside = sum(0.1 <= r["pct"] <= 0.9 for r in rows)
    per = [{"pool": c["pool"], "red": c["red"], "pruebas": c["pruebas"],
            "comisiones": round(c["comisiones_real_vs_p50"], 3), "cobertura": round(c["dentro_p10_p90"], 3)}
           for c in results]
    return Verdict(float(pd.Series(ratios).median()), inside / len(rows), len(rows), ajuste, per)


def reference_pools() -> list[tuple[str, str]]:
    """(pool_id, red) con al menos MIN_DIAS de histórico y TVL actual > MIN_TVL. Sin pares de dos
    estables: su valor final apenas se mueve y la horquilla P10–P90 no mide nada."""
    from . import backtest as B
    con = B._connect()
    try:
        rows = con.execute("""SELECT pool_id, chain, any_value(symbol0), any_value(symbol1) FROM pool_hour_v
                              GROUP BY pool_id, chain
                              HAVING max(hour) - min(hour) >= to_days(?) AND arg_max(tvl_usd, hour) > ?
                              ORDER BY arg_max(tvl_usd, hour) DESC""", [MIN_DIAS, MIN_TVL]).fetchall()
    finally:
        con.close()
    return [(p, c) for p, c, s0, s1 in rows
            if (c, p) not in ANOMALOS and not (s0 in B._STABLE and s1 in B._STABLE)]


def measure(ajuste: float = S.AJUSTE_COMISIONES, pools: list[tuple[str, str]] | None = None) -> Verdict:
    results = []
    for key, chain in pools or reference_pools():
        try:
            results.append(S.calibrar(key, chain, DIAS, ANCHO, HISTORIA, ajuste=ajuste, ultimos=ULTIMOS))
        except Exception as e:                                   # noqa: BLE001 (un pool no tumba el resto)
            print(f"{key} {chain}: {type(e).__name__}: {e}", file=sys.stderr)
    return combine(results, ajuste)


def title(v: Verdict) -> str:
    what = []
    if not v.ok_comisiones:
        what.append(f"comisiones {v.ratio:.2f}")
    if not v.ok_cobertura:
        what.append(f"P10–P90 {v.cobertura:.0%}")
    return "Simulador de pools: recalibrar (" + ", ".join(what) + ")"


def body(v: Verdict, hoy: str) -> str:
    lines = [f"Calibración del {hoy}: aperturas del último año, {DIAS} días, rango ±{ANCHO:.0%}, "
             f"{v.pruebas} pruebas en {len(v.por_pool)} pools.", "",
             f"- **Comisiones reales / media simulada (mediana entre pools): {v.ratio:.2f}** (banda {COMIS_MIN:.2f}–{COMIS_MAX:.2f}) "
             + ("✅" if v.ok_comisiones else "⚠️"),
             f"- **Real dentro del P10–P90: {v.cobertura:.0%}** (banda {COBER_MIN:.0%}–{COBER_MAX:.0%}, ideal 80 %) "
             + ("✅" if v.ok_cobertura else "⚠️"), "",
             "| Pool | Red | Pruebas | Comisiones | P10–P90 |", "|---|---|---:|---:|---:|"]
    lines += [f"| {p['pool']} | {p['red']} | {p['pruebas']} | {p['comisiones']:.2f} | {p['cobertura']:.0%} |"
              for p in v.por_pool]
    lines += [""]
    if not v.ok_comisiones:
        lines += [f"**Qué hacer:** cambiar `AJUSTE_COMISIONES` en `etapas/simulador.py` de {v.ajuste:.2f} a "
                  f"**{v.ajuste_propuesto:.2f}** (o probarlo antes en la pantalla, campo «Ajuste comisiones») y "
                  "reiniciar `python -m etapas.simulador_web`.", ""]
    if not v.ok_cobertura:
        lines += ["**Qué hacer:** la horquilla P10–P90 "
                  + ("se queda corta: la volatilidad reciente es mayor que la del último año. Probar `--historia 1095`."
                     if v.cobertura < COBER_MIN else "es demasiado ancha: probar `--historia 365`.")
                  + " Compruébalo con `python -m etapas.simulador \"WETH/USDC 0.05%\" --red base --calibrar --ultimos 365`.", ""]
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


def decide(v: Verdict, open_n: int | None) -> str | None:
    """abrir | cerrar | None."""
    if not v.ok and open_n is None:
        return "abrir"
    if v.ok and open_n is not None:
        return "cerrar"
    return None


def run(gh: GitHub | None, simulate: bool = False, v: Verdict | None = None) -> str:
    v = v or measure()
    hoy = pd.Timestamp.now().strftime("%Y-%m-%d")
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"fecha": hoy, "ratio": round(v.ratio, 4), "cobertura": round(v.cobertura, 4),
                            "pruebas": v.pruebas, "ajuste": v.ajuste, "ok": v.ok, "pools": v.por_pool},
                           ensure_ascii=False) + "\n")
    summary = (f"{hoy}: comisiones {v.ratio:.2f}, P10–P90 {v.cobertura:.0%}, {v.pruebas} pruebas → "
               + ("en banda" if v.ok else f"DESVIADO (ajuste propuesto {v.ajuste_propuesto:.2f})"))
    open_n = gh.open_issue() if gh else None
    act = decide(v, open_n)
    if act and not simulate and gh:
        if act == "abrir":
            gh.create(title(v), body(v, hoy))
        else:
            gh.close(open_n, f"Calibración del {hoy} en banda: comisiones {v.ratio:.2f}, "
                             f"P10–P90 {v.cobertura:.0%}. Se cierra el aviso.")
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
