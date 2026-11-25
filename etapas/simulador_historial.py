"""Historial de simulaciones: cada rango que simulas en la pantalla se guarda y, cuando vence su plazo, se
compara con lo que pasó de verdad en el pool (MetaProDB). Mide el simulador con TUS consultas.

    python -m etapas.simulador_historial            # evalúa las que han vencido y muestra el resumen
    python -m etapas.simulador_historial --json

Todo es local (out/, fuera de git): pools, rangos y capital no salen de este PC.

- `registrar(res)`: guarda cada rango de un resultado de `simulador.run` (uno por pool, plazo, capital,
  rango y día de los datos: si repites la misma simulación el mismo día, se sustituye).
- `evaluar()`: para las que han vencido, rehace la posición con las horas reales desde el día en que
  simulaste (misma matemática, misma liquidez del pool de aquel día) y anota lo real.
- `resumen()`: en cuántas lo real cayó dentro del P10–P90, el error medio (real − media simulada) y si el
  veredicto «compensa» / «no compensa» acertó.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

import pandas as pd

from . import backtest as B
from . import simulador as S

OUT = Path(__file__).resolve().parent.parent / "out"
LOG = OUT / "simulador_historial.jsonl"
RES = OUT / "simulador_historial_res.jsonl"
MAX_EVALUAR = 60                    # por llamada (cada una lee el pool de MetaProDB)
_lock = threading.Lock()


def _leer(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _escribir(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def compensa(vs_mantener: float, resultado: float, sin_riesgo: float) -> bool:
    """El mismo criterio que la Recomendación: gana a guardar los tokens y a la referencia sin riesgo."""
    return vs_mantener > 0 and resultado > sin_riesgo


def registrar(res: dict, log: Path = LOG) -> int:
    """Guarda los rangos de una simulación. Devuelve cuántos."""
    pool, cap, dias = res["pool"], res["capital"], res["dias"]
    ref = res.get("sin_riesgo")
    sr = (ref["valor_final"] - cap) if ref else 0.0
    dia = str(res["hora"])[:10]
    nuevos = []
    for r in res["rangos"]:
        vm = r["desglose"]["total"] - r["desglose"]["precio"]
        key = f"{pool['chain']}|{pool['pool_id']}|{dias}|{cap:g}|{r['tl']}|{r['tu']}|{dia}"
        nuevos.append({"key": key, "fecha": str(res["hora"]), "pool_id": pool["pool_id"], "chain": pool["chain"],
                       "pool": pool["label"], "par": res.get("par"), "dias": dias, "capital": cap,
                       "texto": r["texto"], "tl": r["tl"], "tu": r["tu"], "lo": r["lo"], "hi": r["hi"],
                       "precio": res["precio"], "liquidez": res.get("liquidez_activa", 0.0), "gas": res.get("gas"),
                       "tendencia": res.get("tendencia"), "ajuste": res.get("ajuste_comisiones"),
                       "valor_media": r["valor_media"], "p10": r["valor_final"]["p10"], "p50": r["valor_final"]["p50"],
                       "p90": r["valor_final"]["p90"], "vs_media": vm, "sin_riesgo": sr,
                       "compensa": compensa(vm, r["valor_media"] - cap, sr),
                       "fiabilidad": (res.get("fiabilidad") or {}).get("nivel")})
    with _lock:
        rows = {x["key"]: x for x in _leer(log)}
        for n in nuevos:
            rows[n["key"]] = n
        _escribir(log, list(rows.values()))
    return len(nuevos)


def _real(rec: dict) -> dict | None:
    """Lo que pasó de verdad con ese rango desde el día en que se simuló; None si aún no ha vencido."""
    ini = pd.Timestamp(rec["fecha"])
    fin = ini + pd.Timedelta(days=rec["dias"])
    try:
        pool, df = B.load_hours(rec["pool_id"], rec["chain"], ini.strftime("%Y-%m-%d %H:00"), fin.strftime("%Y-%m-%d %H:00"))
    except B.BacktestError:
        return None
    if df["hour"].iloc[-1] < fin - pd.Timedelta(hours=2) or len(df) < rec["dias"] * 24 * 0.9:
        return None
    h = S.features(pool, df)
    h.liquidity_now = rec.get("liquidez") or 0.0
    rg = S.Rango(rec["texto"], int(rec["tl"]), int(rec["tu"]), rec["lo"], rec["hi"])
    s = S.simulate(h, S.replay(h), rg, rec["capital"], rec.get("gas"))
    real, vs = float(s.final[0]), float(s.final[0] - s.hodl[0])
    cap = rec["capital"]
    return {"key": rec["key"], "real": real, "real_vs": vs, "error": real - rec["valor_media"],
            "dentro": rec["p10"] <= real <= rec["p90"],
            "lado": "debajo" if real < rec["p10"] else "encima" if real > rec["p90"] else "dentro",
            "real_compensa": compensa(vs, real - cap, rec["sin_riesgo"]),
            "evaluada": str(pd.Timestamp.now().floor("min"))}


def evaluar(log: Path = LOG, res: Path = RES, maximo: int = MAX_EVALUAR, real=_real) -> int:
    """Evalúa las simulaciones que han vencido y aún no tienen resultado. Devuelve cuántas evaluó."""
    hechas = {r["key"] for r in _leer(res)}
    ahora = pd.Timestamp.now()
    pend = [r for r in _leer(log) if r["key"] not in hechas
            and pd.Timestamp(r["fecha"]) + pd.Timedelta(days=r["dias"]) <= ahora]
    nuevas = []
    for rec in pend[:maximo]:
        try:
            x = real(rec)
        except Exception as e:                                   # noqa: BLE001 (una no tumba el resto)
            print(f"{rec['pool']} {rec['fecha']}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if x:
            nuevas.append(x)
    if nuevas:
        with _lock:
            _escribir(res, _leer(res) + nuevas)
    return len(nuevas)


def resumen(log: Path = LOG, res: Path = RES, ultimas: int = 12) -> dict:
    recs = {r["key"]: r for r in _leer(log)}
    ev = [r | recs[r["key"]] for r in _leer(res) if r["key"] in recs]
    ahora = pd.Timestamp.now()
    pendientes = [r for k, r in recs.items() if k not in {e["key"] for e in ev}]
    proxima = min((pd.Timestamp(r["fecha"]) + pd.Timedelta(days=r["dias"]) for r in pendientes), default=None)
    out = {"guardadas": len(recs), "evaluadas": len(ev), "pendientes": len(pendientes),
           "proxima": str(proxima)[:16] if proxima is not None else None}
    if not ev:
        return out
    d = pd.DataFrame(ev)
    out |= {"dentro_p10_p90": float(d["dentro"].mean()), "debajo_p10": float((d["lado"] == "debajo").mean()),
            "encima_p90": float((d["lado"] == "encima").mean()),
            "error_medio": float(d["error"].mean()), "error_medio_pct": float((d["error"] / d["capital"]).mean()),
            "error_abs_medio_pct": float((d["error"].abs() / d["capital"]).mean()),
            "plazos": {int(k): int(v) for k, v in d["dias"].value_counts().sort_index().items()},
            "pools": int(d["pool"].nunique()),
            "veredicto": {lado: {"n": int(len(g)), "acierto": float((g["compensa"] == g["real_compensa"]).mean())}
                          for lado, g in (("compensa", d[d["compensa"]]), ("no_compensa", d[~d["compensa"]])) if len(g)},
            "ultimas": [{k: r[k] for k in ("fecha", "pool", "chain", "dias", "capital", "texto", "valor_media", "p10", "p90",
                                           "real", "dentro", "compensa", "real_compensa")}
                        for r in sorted(ev, key=lambda r: r["fecha"], reverse=True)[:ultimas]]}
    return out


def texto(r: dict) -> str:
    if not r["evaluadas"]:
        return (f"{r['guardadas']} simulaciones guardadas, ninguna ha vencido aún"
                + (f" (la primera vence el {r['proxima']})." if r.get("proxima") else "."))
    v = r.get("veredicto", {})
    vt = "; ".join(f"«{'compensa' if k == 'compensa' else 'no compensa'}» acertó {x['acierto'] * 100:.0f} % de {x['n']}"
                   for k, x in v.items())
    err = f"{r['error_medio_pct'] * 100:+.1f}".replace(".", ",").replace("-", "−")
    return (f"{r['evaluadas']} simulaciones vencidas ({r['pools']} pools), {r['pendientes']} pendientes. Lo real cayó "
            f"dentro del P10–P90 el {r['dentro_p10_p90'] * 100:.0f} % (lo ideal, ~80 %); error medio {err} % del "
            f"capital (real − media simulada). Veredicto: {vt}.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compara las simulaciones guardadas con lo que pasó de verdad.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    n = evaluar()
    r = resumen()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
    else:
        print(f"Evaluadas ahora: {n}.\n" + texto(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
