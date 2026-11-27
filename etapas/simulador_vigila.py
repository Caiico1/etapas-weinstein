"""Vigilancia mensual de las piezas validadas una vez: stop, cobertura con perpetuo, plan y contraste con Revert.

    python -m etapas.simulador_vigila            # mide y enseña el resultado (no toca GitHub)

La tarea mensual (`simulador_aviso`) lo lanza junto a la calibración de comisiones y horquilla y abre la
issue si alguna pieza se sale de su banda. Igual que la validación original: pools de referencia,
aperturas del último año cada 7 días a 30 días, con los datos de antes de cada fecha frente al replay real
(y el funding real de Bybit hora a hora):
  - stop al salirse por abajo con ±10 %: veces que salta, simulado frente a real;
  - corto en el perpetuo del todo con ±10 %: funding simulado frente a real y error de la media;
  - plan de mejor media (`plan.planes`) frente a ±10 % sin stop ni cobertura, en lo real;
  - contraste con Revert: pools que salen de ×0,5–2 desde el contraste anterior.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import backtest as B
from . import perpetuo as P
from . import plan as PL
from . import simulador as S

DIAS, ULTIMOS, PASO, ESCENARIOS, CAPITAL = 30, 365, 7, 300, 5000.0
MAX_DIF_STOP = 0.10                 # veces que salta: |simulado − real| como mucho 10 puntos
BANDA_FUNDING = (0.6, 1.4)          # funding simulado ÷ real
MIN_FUNDING = 1.0                   # $ medios por apertura: con menos, el cociente es ruido
MAX_ERROR_CUBIERTO = 0.02           # error medio de lo cubierto del todo, en fracción del capital
MIN_PLAN = 0.0                      # el plan de mejor media no debe perder frente a ±10 % en lo real
MAX_NUEVOS_FUERA = 2                # pools que salen de ×0,5–2 frente a Revert desde el contraste anterior


@dataclass
class Pieza:
    nombre: str
    valor: str
    banda: str
    ok: bool
    que_hacer: str = ""
    pruebas: int = 0

    def __post_init__(self):
        self.ok, self.pruebas = bool(self.ok), int(self.pruebas)


def _uno(args: tuple) -> list[dict]:
    key, chain, f = args
    out = []
    try:
        full = S.load(key, chain, historia=100_000)
        df, pool = full.df, full.pool
        sim = P.simbolo(pool)
        _, _, asset0 = B.pair_of(pool)
        if sim:
            asset0 = sim[1]
        gas = S.GAS.get(chain, 0.05)
        hours = df["hour"]
        last = hours.iloc[-1]
        t = last - pd.Timedelta(days=ULTIMOS + DIAS)
        while t + pd.Timedelta(days=DIAS) <= last:
            past = df[(hours > t - pd.Timedelta(days=S.HISTORIA_DIAS)) & (hours <= t)].reset_index(drop=True)
            fut = df[(hours >= t) & (hours <= t + pd.Timedelta(days=DIAS))].reset_index(drop=True)
            if len(past) > S.HISTORIA_DIAS * 24 * 0.8 and len(fut) >= DIAS * 24 * 0.9:
                hp, hf = S.features(pool, past), S.features(pool, fut)
                hf.liquidity_now = hp.liquidity_now
                paths = S.bootstrap(hp, len(fut) - 1, ESCENARIOS, seed=None)
                rp = S.replay(hf)
                cubrir = f is not None and t >= f.index.min() + pd.Timedelta(days=95)
                m, apr, fh = 1.0, 0.0, None
                if cubrir:
                    m = P.margen(S.hf_historico(hp, DIAS, asset0))
                    apr = P.apr_reciente(f, hp.hour_now)
                    fh = P.funding_horas(f, fut["hour"])
                fila = {"pool": pool["label"], "red": chain, "fecha": str(t.date())}
                mejor, base = None, None
                cands = PL.candidatos(hp, cubrir)
                fijos = [(0.10, "sin stop", 0.0), (0.10, "al salirse por abajo", 0.0)] + ([(0.10, "sin stop", 1.0)] if cubrir else [])
                for w, st, c in cands + [x for x in fijos if x not in cands]:
                    rg = S.parse_rango(f"{w * 100:g}", hp)
                    x = PL.STOPS_PLAN[st]
                    s = PL.simulate_plan(hp, paths, rg, x, c, CAPITAL, gas, asset0, m, apr, apy=0.04)
                    r = PL.simulate_plan(hf, rp, rg, x, c, CAPITAL, gas, asset0, m, apr, funding_h=fh, apy=0.04)
                    if (w, st, c) == (0.10, "al salirse por abajo", 0.0):
                        fila |= {"stop_sim": float(s["salta"].mean()), "stop_real": float(r["salta"][0])}
                    if (w, st, c) == (0.10, "sin stop", 1.0):
                        fila |= {"cub_sim": float(s["final"].mean()), "cub_real": float(r["final"][0]),
                                 "fund_sim": float(s["funding"].mean()), "fund_real": float(r["funding"][0])}
                    if (w, st, c) == (0.10, "sin stop", 0.0):
                        base = float(r["final"][0])
                    elif (w, st, c) in cands and (mejor is None or s["final"].mean() > mejor[0]):
                        mejor = (float(s["final"].mean()), float(r["final"][0]))
                if mejor and base is not None:
                    fila |= {"plan_real": mejor[1], "base_real": base}
                out.append(fila)
            t += pd.Timedelta(days=PASO)
    except Exception as e:                                       # noqa: BLE001 (un pool no tumba el resto)
        print(f"vigila {key} {chain}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def medir(pools: list[tuple[str, str]] | None = None, procesos: int = 4, funding=None) -> pd.DataFrame:
    from . import simulador_aviso as A
    pools = pools if pools is not None else A.reference_pools(A.min_dias(DIAS))
    fund = {}
    for k, c in pools:
        try:
            sim = P.simbolo(S.load(k, c, 400).pool)
        except Exception:                                        # noqa: BLE001
            sim = None
        if sim and sim[0] not in fund:
            try:
                fund[sim[0]] = funding(sim[0]) if funding else P.funding(sim[0])
            except OSError as e:
                print(f"vigila: sin funding de {sim[0]} ({type(e).__name__})", file=sys.stderr)
                fund[sim[0]] = None
        fund[(k, c)] = fund.get(sim[0]) if sim else None
    jobs = [(k, c, fund[(k, c)]) for k, c in pools]
    if procesos <= 1:
        res = [_uno(j) for j in jobs]
    else:
        with ProcessPoolExecutor(procesos) as ex:
            res = list(ex.map(_uno, jobs))
    return pd.DataFrame([r for rs in res for r in rs])


def nuevos_fuera(antes: dict | None, ahora: dict | None) -> list[str]:
    """Pools que están fuera de la banda baja de Revert ahora y no lo estaban en el contraste anterior."""
    from . import contraste_revert as CR
    lo, hi = CR.BANDA_BAJA

    def fuera(r):
        return {k: x["label"] for k, x in (r or {}).get("pools", {}).items()
                if (x.get("ano") or {}).get("ratio") and not lo <= x["ano"]["ratio"] <= hi}
    a = fuera(antes)
    return sorted(v for k, v in fuera(ahora).items() if k not in a) if antes else []


def comprobar(d: pd.DataFrame, revert_nuevos: list[str] | None = None) -> list[Pieza]:
    out = []
    if "stop_sim" in d and d["stop_sim"].notna().any():
        x = d.dropna(subset=["stop_sim"])
        s, r = x["stop_sim"].mean(), x["stop_real"].mean()
        out.append(Pieza("Stop (salta, simulado / real)", f"{s:.0%} / {r:.0%}", f"diferencia ≤ {MAX_DIF_STOP:.0%}",
                         abs(s - r) <= MAX_DIF_STOP,
                         "El stop salta en la realidad con otra frecuencia que en los escenarios: revisa "
                         "`simulate_salida` y el bloque del bootstrap antes de fiarte del stop sugerido.", len(x)))
    if "cub_sim" in d and d["cub_sim"].notna().any():
        x = d.dropna(subset=["cub_sim"])
        fs, fr = x["fund_sim"].mean(), x["fund_real"].mean()
        ratio = fs / fr if abs(fr) >= MIN_FUNDING else None
        okf = ratio is None or BANDA_FUNDING[0] <= ratio <= BANDA_FUNDING[1]
        out.append(Pieza("Funding del corto (simulado ÷ real)", "—" if ratio is None else f"×{ratio:.2f}",
                         f"×{BANDA_FUNDING[0]:.1f}–{BANDA_FUNDING[1]:.1f}", okf,
                         "El funding medio de 90 días ya no anticipa el real: revisa `perpetuo.FUNDING_DIAS`.", len(x)))
        err = float((x["cub_real"] - x["cub_sim"]).mean())
        out.append(Pieza("Cubierto del todo (error de la media)", f"{err:+.0f} $ por {CAPITAL:,.0f} $".replace(",", "."),
                         f"±{MAX_ERROR_CUBIERTO * CAPITAL:.0f} $", abs(err) <= MAX_ERROR_CUBIERTO * CAPITAL,
                         "La cobertura con perpetuo se desvía de lo real: revisa margen, funding y liquidación en "
                         "`perpetuo.cubierta`.", len(x)))
    if "plan_real" in d and d["plan_real"].notna().any():
        x = d.dropna(subset=["plan_real"])
        dif = float((x["plan_real"] - x["base_real"]).mean())
        out.append(Pieza("Plan de mejor media frente a ±10 % (real)", f"{dif:+.0f} $", f"≥ {MIN_PLAN:+.0f} $",
                         dif >= MIN_PLAN,
                         "El plan de mejor media perdió en lo real frente a ±10 % sin más en el último año: "
                         "revisa `plan.ANCHOS_PLAN` y el criterio antes de seguir sus frases.", len(x)))
    if revert_nuevos is not None:
        out.append(Pieza("Pools que dejan de cuadrar con Revert", str(len(revert_nuevos)), f"≤ {MAX_NUEVOS_FUERA}",
                         len(revert_nuevos) <= MAX_NUEVOS_FUERA,
                         "Varios pools han dejado de cuadrar con Revert desde el mes pasado (el semáforo ya los marca "
                         "con fiabilidad baja): mira `python -m etapas.contraste_revert` y el ingestor de MetaProDB.",
                         len(revert_nuevos)))
    return out


def texto(ps: list[Pieza]) -> str:
    return " · ".join(f"{p.nombre}: {p.valor}" + ("" if p.ok else " ⚠") for p in ps)


def tabla(ps: list[Pieza]) -> list[str]:
    """Filas de la issue (solo cifras agregadas: el repositorio es público)."""
    lines = ["| Pieza | Último año | Banda | Pruebas | |", "|---|---:|---|---:|---|"]
    lines += [f"| {p.nombre} | {p.valor} | {p.banda} | {p.pruebas} | {'✅' if p.ok else '⚠️'} |" for p in ps]
    lines += [""]
    lines += [f"**{p.nombre}:** {p.que_hacer}" for p in ps if not p.ok]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    ps = comprobar(medir())
    for p in ps:
        print(f"{'ok' if p.ok else '⚠ '} {p.nombre}: {p.valor} (banda {p.banda}, {p.pruebas} pruebas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
