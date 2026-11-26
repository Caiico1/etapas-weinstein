"""Ranking de los pools de MetaProDB con el simulador: ¿en cuál añade más ser LP frente a guardar los tokens?

    python -m etapas.ranking                 # 30 días, 5.000 $; guarda out/ranking.json
    python -m etapas.ranking --dias 7 --json

Cada pool con datos recientes, TVL ≥ 0,5 M$ y ≥ 1 año de histórico (sin pares de dos estables ni los de
datos anómalos) se simula a `dias` con su mejor ancho (`barrido`, criterio media) y se ordena por «vs
mantener» medio: lo que añade la posición (comisiones − pérdida impermanente − costes) frente a guardar
los mismos tokens. También da el valor final medio y el escenario malo frente a la referencia sin riesgo,
pero NO ordena por eso: el simulador no predice hacia dónde irá el precio.

Validado el 26-nov-2026 con los datos corregidos (sin horas de pool vacío y sin las horas de comisiones fantasma
de los pares pegados, comprobadas contra Revert; 61 pools, 53 fechas cada 14 días de dos años, a 30 días, datos
solo del pasado):
  - por «vs mantener», los 10 primeros quedaron de verdad en −10 $ por 5.000 $ frente a −23 $ de media (−39 $
    los 10 últimos) y superaron a la media en el 66 % de las fechas; con TVL ≥ 2 M$, −11 $ frente a −24 $ y el
    70 % de las fechas (69 % y 70 % cada año). Por quintiles, lo real sube con la puntuación: −41, −27, −24,
    −14 y −8 $. Ayuda a perder menos frente a mantener; ninguno gana de media;
  - ordenar por comisiones/TVL de los últimos 7 días no lo consigue (−17 $; con TVL ≥ 2 M$, −26 $, peor que la
    media);
  - por resultado total (con el precio) ninguno ordena: correlación ≈ 0.
  (Con los datos sin corregir salía +14 $ frente a −13 $: la ventaja venía de comisiones fantasma que estaban a
  la vez en lo simulado y en lo «real».)
Se excluyen los de fiabilidad baja (entre ellos, los de comisiones concentradas en unas pocas horas, ver
`simulador.MAX_CONCENTRACION`) y, como «datos sospechosos», los que cobrarían > 15 % al mes o cuyo «vs
mantener» supera el capital.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import backtest as B
from . import simulador as S

OUT = Path(__file__).resolve().parent.parent / "out"
CACHE = OUT / "ranking.json"
MIN_TVL = 500_000
MIN_HIST = 365
MAX_COMISIONES_MES = 0.15
TOP_VALIDADO = 10
_lock = threading.Lock()
_estado = {"calculando": False, "hechos": 0, "total": 0, "error": None}


def _estables() -> set[str]:
    return {s.upper() for s in B._STABLE}


def candidatos(todos: list[dict] | None = None) -> list[dict]:
    from .simulador_aviso import ANOMALOS
    est = _estables()
    todos = S.pools_recientes() if todos is None else todos
    return [p for p in todos if (p["tvl"] or 0) >= MIN_TVL and (p["chain"], p["pool_id"]) not in ANOMALOS
            and not (p["symbol0"].upper() in est and p["symbol1"].upper() in est)]


def puntuar(p: dict, dias: int = 30, capital: float = 5000, escenarios: int = 500, cargar=None,
            sin_riesgo: dict | None = None) -> dict:
    """Mejor ancho del pool y sus cifras; {"excluido": motivo} si no entra."""
    cargar = cargar or (lambda pid, ch: S.load(pid, ch, S.HISTORIA_DIAS))
    base = {k: p[k] for k in ("chain", "version", "pool_id", "label", "tvl")}
    h = cargar(p["pool_id"], p["chain"])
    if h.dias_historia < MIN_HIST:
        return base | {"excluido": f"solo {h.dias_historia:.0f} días de histórico"}
    fiab = S.fiabilidad(h, dias, S.calibracion_pool(p["pool_id"], p["chain"], dias))
    if fiab["nivel"] == "baja":
        return base | {"excluido": "fiabilidad baja: " + "; ".join(fiab["motivos"])[:160]}
    paths = S.bootstrap(h, dias * 24, escenarios)
    best = S.mejor_rango(S.barrido(h, paths, capital), "media")
    if (not np.isfinite(best["vs_media"]) or best["comisiones"] / capital > MAX_COMISIONES_MES * dias / 30
            or abs(best["vs_media"]) > capital):
        return base | {"excluido": "datos sospechosos (comisiones de MetaProDB imposibles)"}
    asset, quote, _ = B.pair_of(h.pool)
    est = _estables()
    if asset.upper() in est and quote.upper() in est:
        return base | {"excluido": "par de dos estables (fuera de lo validado)"}
    sr = sin_riesgo if sin_riesgo is not None else (S.referencia_sin_riesgo(p["chain"]) or {})
    sr_final = capital * (1 + sr.get("apy", 0.0)) ** (dias / 365)
    return base | {"par": f"{asset}/{quote}", "precio": S.price_now(h), "ancho": best["texto"], "lo": best["lo"],
                   "hi": best["hi"], "vs_media": best["vs_media"], "vs_mediana": best["vs_mediana"],
                   "comisiones": best["comisiones"], "en_rango": best["en_rango"], "valor_media": best["valor_media"],
                   "valor_p10": best["valor_p10"], "vs_sin_riesgo": best["valor_media"] - sr_final,
                   "fiabilidad": fiab["nivel"],
                   "nivel_comisiones": h.nivel(S.NIVEL_DIAS)}


def calcular(dias: int = 30, capital: float = 5000, escenarios: int = 500, todos: list[dict] | None = None,
             cargar=None, progreso=None) -> dict:
    cands = candidatos(todos)
    refs: dict[str, dict] = {}
    filas, fuera = [], []
    for i, p in enumerate(cands):
        if p["chain"] not in refs:
            refs[p["chain"]] = S.referencia_sin_riesgo(p["chain"]) or {}
        try:
            r = puntuar(p, dias, capital, escenarios, cargar, refs[p["chain"]])
        except Exception as e:                                   # noqa: BLE001 (un pool no tumba el ranking)
            r = {k: p[k] for k in ("chain", "version", "pool_id", "label", "tvl")} | {"excluido": f"{type(e).__name__}: {e}"}
        (fuera if "excluido" in r else filas).append(r)
        if progreso:
            progreso(i + 1, len(cands))
    filas.sort(key=lambda r: -r["vs_media"])
    return {"fecha": time.strftime("%Y-%m-%d %H:%M"), "dias": dias, "capital": capital, "escenarios": escenarios,
            "pools": filas, "excluidos": fuera, "top_validado": TOP_VALIDADO,
            "sin_riesgo": {c: r.get("nombre") for c, r in refs.items()}}


def guardado(path: Path = CACHE) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def guardar(r: dict, path: Path = CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, default=float), encoding="utf-8")
    tmp.replace(path)


def estado() -> dict:
    return dict(_estado)


def lanzar(dias: int = 30, capital: float = 5000) -> bool:
    """Calcula el ranking en segundo plano (para la pantalla). False si ya hay uno en marcha."""
    with _lock:
        if _estado["calculando"]:
            return False
        _estado.update(calculando=True, hechos=0, total=0, error=None)

    def prog(i, n):
        _estado.update(hechos=i, total=n)

    def run():
        try:
            guardar(calcular(dias, capital, progreso=prog))
        except Exception as e:                                   # noqa: BLE001
            _estado["error"] = f"{type(e).__name__}: {e}"
        finally:
            _estado["calculando"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--capital", type=float, default=5000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = calcular(a.dias, a.capital, progreso=lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr))
    print(file=sys.stderr)
    guardar(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=float))
        return 0
    print(f"Ranking a {a.dias} días, {a.capital:,.0f} $ · {len(r['pools'])} pools ({len(r['excluidos'])} excluidos) · "
          f"ordenado por «vs mantener» medio (lo que añade ser LP frente a guardar los tokens)")
    for i, p in enumerate(r["pools"][:25], 1):
        print(f"{i:>3}. {p['label']:<22} {p['chain']:<9} {p['ancho']:>6}  vs mantener {p['vs_media']:+8.1f} $  "
              f"valor medio {p['valor_media']:8.0f} $  P10 {p['valor_p10']:8.0f} $  vs sin riesgo {p['vs_sin_riesgo']:+7.0f} $  "
              f"{p['fiabilidad']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
