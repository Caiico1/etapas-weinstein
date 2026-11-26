"""Contraste de las comisiones de MetaProDB con Revert, una fuente independiente.

    python -m etapas.contraste_revert            # todos los pools con datos recientes; guarda out/contraste_revert.json
    python -m etapas.contraste_revert --json

Todas las validaciones del simulador comparan lo simulado con un replay «real» hecho con los mismos datos de
MetaProDB: si esos datos están mal, la validación no lo ve. Aquí se comparan las comisiones de los LP de cada
pool en el último año (y mes) según Revert —calculadas con sus propios eventos indexados (`discover_pools`,
MCP público, solo lectura)— con las de MetaProDB en la misma ventana (Δfee_growth × liquidez activa), ya
limpias como las lee el simulador: sin horas con el pool vacío y, en los pools de comisiones concentradas,
sin las horas pico (`simulador.quitar_picos`).

Medido el 26-nov-2026 (166 pools): en los pools grandes y limpios MetaProDB da ×0,77–0,95 de Revert. Es lo
esperable: MetaProDB mide lo que cobra un LP pasivo por unidad de liquidez, y Revert suma también lo que se
llevan los LP «just-in-time» que entran y salen en el mismo bloque; el factor de ajuste de comisiones del
simulador ya lo recoge. En los pares pegados MetaProDB daba ×20–270 por las horas pico; sin ellas, ×0,8–1,3.

`simulador.fiabilidad` usa el resultado: fuera de ×0,5–×2, fiabilidad baja; fuera de ×0,67–×1,5, media.
Lo refresca la tarea mensual de calibración (`simulador_aviso`). Todo local (out/, fuera de git).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as B
from . import simulador as S

OUT = Path(__file__).resolve().parent.parent / "out"
CACHE = OUT / "contraste_revert.json"
RED = {"ethereum": "mainnet", "arbitrum": "arbitrum", "base": "base"}
MIN_FEES = 1000.0                   # con menos comisiones en Revert, el cociente es ruido
BANDA_MEDIA, BANDA_BAJA = (0.67, 1.5), (0.5, 2.0)


def _mdb(con, pool: dict, t0: pd.Timestamp, t1: pd.Timestamp, picos: bool) -> float:
    """Comisiones de los LP en MetaProDB en [t0, t1), sin horas de pool vacío y, si `picos`, sin las horas
    de más de PICO_COMISIONES × la mediana (por unidad de liquidez, en $)."""
    r = con.execute("""
        WITH v AS (SELECT fees_usd,
                     fee_growth0 * coalesce(usd0, 0) + fee_growth1 * coalesce(usd1, 0) AS g
                   FROM pool_hour_v WHERE chain = ? AND pool_id = ? AND hour >= ? AND hour < ?
                     AND coalesce(liquidity, 1) > 0 AND abs(tick) < ? AND coalesce(tvl_usd, 1e9) >= ?),
             m AS (SELECT median(g) FILTER (WHERE g > 0) AS gm FROM v)
        SELECT sum(fees_usd) FILTER (WHERE NOT ? OR g <= ? * (SELECT gm FROM m)) FROM v""",
        [pool["chain"], pool["pool_id"], t0, t1, B.EMPTY_TICK, B.MIN_TVL_POOL, picos, S.PICO_COMISIONES]).fetchone()
    return float(r[0] or 0.0)


def contrastar(pool: dict, cliente, con, cargar=None) -> dict:
    cargar = cargar or (lambda pid, ch: S.load(pid, ch, S.HISTORIA_DIAS))
    out = {"label": pool["label"], "chain": pool["chain"], "version": pool["version"]}
    try:
        picos = cargar(pool["pool_id"], pool["chain"]).picos_quitados > 0
    except Exception:                                            # noqa: BLE001
        picos = False
    out["picos_quitados"] = picos
    for per, nombre in (("year", "ano"), ("month", "mes")):
        d = cliente.tool("discover_pools", network=RED[pool["chain"]], pool_address=pool["pool_id"], period=per)
        data = d.get("data") if isinstance(d, dict) else d
        if not data:
            continue
        x = data[0]
        w = x["metrics_window"]
        t0, t1 = pd.Timestamp(w["period_start"], unit="s"), pd.Timestamp(w["period_end"], unit="s")
        rv = float(x["fees_usd"] or 0)
        mv = _mdb(con, pool, t0, t1, picos)
        out[nombre] = {"revert": rv, "metaprodb": mv, "ratio": mv / rv if rv >= MIN_FEES else None,
                       "desde": str(t0), "hasta": str(t1)}
    return out


def calcular(pools: list[dict] | None = None, cliente=None, cargar=None, progreso=None) -> dict:
    from .posiciones_lp import RevertMCP
    pools = S.pools_recientes() if pools is None else pools
    cliente = cliente or RevertMCP()
    con = B._connect()
    res = {}
    try:
        for i, p in enumerate(pools):
            try:
                res[f"{p['chain']}|{p['pool_id']}"] = contrastar(p, cliente, con, cargar)
            except Exception as e:                               # noqa: BLE001 (un pool no tumba el resto)
                res[f"{p['chain']}|{p['pool_id']}"] = {"label": p["label"], "chain": p["chain"], "error": f"{type(e).__name__}: {e}"}
            if progreso:
                progreso(i + 1, len(pools))
    finally:
        con.close()
    return {"fecha": time.strftime("%Y-%m-%d %H:%M"), "pools": res}


def guardar(r: dict, path: Path = CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def de_pool(pool_id: str, chain: str, path: Path | None = None) -> dict | None:
    """El contraste guardado de un pool (el del último año), o None."""
    try:
        r = json.loads((path or CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    x = r.get("pools", {}).get(f"{chain}|{pool_id}")
    if not x or not (x.get("ano") or {}).get("ratio"):
        return None
    return {"ratio": x["ano"]["ratio"], "revert": x["ano"]["revert"], "metaprodb": x["ano"]["metaprodb"],
            "fecha": r.get("fecha"), "picos_quitados": x.get("picos_quitados", False)}


def resumen(r: dict) -> dict:
    rs = [x["ano"]["ratio"] for x in r["pools"].values() if (x.get("ano") or {}).get("ratio")]
    a = np.array(rs) if rs else np.array([np.nan])
    return {"pools": len(rs), "mediana": float(np.nanmedian(a)),
            "dentro_media": float(np.mean((a >= BANDA_MEDIA[0]) & (a <= BANDA_MEDIA[1]))),
            "fuera_baja": float(np.mean((a < BANDA_BAJA[0]) | (a > BANDA_BAJA[1])))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = calcular(progreso=lambda i, n: print(f"\r{i}/{n}", end="", file=sys.stderr))
    print(file=sys.stderr)
    guardar(r)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    s = resumen(r)
    print(f"{s['pools']} pools con comisiones en Revert: MetaProDB ÷ Revert mediana ×{s['mediana']:.2f}; "
          f"{s['dentro_media'] * 100:.0f} % dentro de ×0,67–1,5; {s['fuera_baja'] * 100:.0f} % fuera de ×0,5–2")
    filas = sorted(((x["ano"]["ratio"], x["label"], x["chain"], x["version"]) for x in r["pools"].values()
                    if (x.get("ano") or {}).get("ratio")), reverse=True)
    for ratio, label, chain, ver in filas:
        if not BANDA_MEDIA[0] <= ratio <= BANDA_MEDIA[1]:
            print(f"  ×{ratio:8.2f}  {label} ({chain} {ver})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
