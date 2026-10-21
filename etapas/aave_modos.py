"""Préstamo en Aave V3 que cambia de modo según la etapa semanal (Weinstein) de un activo, contra el
histórico horario de MetaProDB (tablas aave_*).

Se deposita `capital` de garantía y se pide `deuda` dólares, que salen de Aave (es el préstamo que se
quiere amortizar). Tres modos, con el activo volátil A (cbBTC) y la estable S (USDC):
  alcista: garantía A, deuda S  → gana si A sube (la deuda no cambia);
  bajista: garantía S, deuda A  → gana si A baja (la deuda vale menos);
  neutral: garantía S, deuda S  → sin exposición; cuesta la diferencia entre pedir y depositar S.
Las señales se calculan solo con las velas cerradas hasta ese momento (Binance) y el cambio se hace en
la primera hora tras el cierre de la vela. Reglas:
  semanal: etapa semanal 2 → alcista, 4 → bajista, 1 y 3 → se queda en el modo que tenía;
  semanal-neutral: igual, pero las etapas 1 y 3 → neutral;
  diaria:  lo mismo con la etapa diaria;
  semanal-mixto | diaria-mixto: etapas 1 y 3 → mixto (garantía 50 % A y 50 % S, deuda en S);
  combinada: semanal y diaria en 2 → alcista; las dos en 4 → bajista; si no coinciden → mixto;
  nivel:   cierre semanal por debajo del nivel de abajo de la semana anterior (el que invalida la etapa 2,
           o el suelo del rango en 1 y 3) → bajista; por encima del nivel de arriba (el que invalida la
           etapa 4, o el techo del rango) → alcista; entre los dos, se queda;
  siempre-alcista | siempre-mixto | siempre-bajista | siempre-neutral: sin cambios (referencias).
La garantía y la deuda pueden mezclar los dos tokens (Aave admite varias garantías y varias deudas).
Cambiar la garantía (Collateral Swap) cuesta --coste-garantia pb de lo que vale; cambiar la deuda (Debt
Swap) --coste-deuda pb de lo que vale, más gas por transacción. Liquidación y defensa como en
aave_backtest.simulate. Medida clave: «amortizado» = (patrimonio final − inicial) / deuda inicial, con
patrimonio = garantía − deuda − dinero aportado para defender − costes.

    python -m etapas.aave_modos cbBTC USDC --red base --capital 11500 --deuda 3450 --ventana 180,365 --paso 7

Límites: precio y HF vistos cada hora; los cambios se valoran al precio del oráculo de Aave más el coste
indicado (sin el desvío real del DEX); la etapa usa Binance BTC/USDT, no el precio de cbBTC.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .aave_backtest import CLOSE_FACTOR, FULL_BELOW_HF, SMALL_USD, _reserve
from .backtest import BacktestError, _connect
from .classifier import classify, levels
from .config import DISCLAIMER, TIMEFRAMES
from .data import fetch_candles

MODES = {   # modo → (parte de la garantía en A, parte de la deuda en A); el resto, en S
    "alcista": (1.0, 0.0), "mixto": (0.5, 0.0), "neutral": (0.0, 0.0), "bajista": (0.0, 1.0)}
TREND = {2: "alcista", 4: "bajista"}
MIXED = {1: "mixto", 2: "alcista", 3: "mixto", 4: "bajista"}
RULES = {   # regla → (columna de la señal, etapa → modo) | modo fijo
    "semanal": ("st_w", TREND), "semanal-neutral": ("st_w", {1: "neutral", 2: "alcista", 3: "neutral", 4: "bajista"}),
    "semanal-mixto": ("st_w", MIXED), "diaria": ("st_d", TREND), "diaria-mixto": ("st_d", MIXED),
    "nivel": ("nivel", None), "combinada": ("st_wd", None),
    "siempre-alcista": "alcista", "siempre-mixto": "mixto", "siempre-bajista": "bajista", "siempre-neutral": "neutral",
}


# --- datos -------------------------------------------------------------------------------------

def load(asset: str, stable: str, chain: str, market: str | None = None) -> tuple[dict, pd.DataFrame]:
    """Una fila por hora con índices, precio y parámetros de los dos tokens (prefijos a_ y s_)."""
    market = market or chain
    con = _connect()
    try:
        a_asset, a_sym = _reserve(con, chain, market, asset)
        s_asset, s_sym = _reserve(con, chain, market, stable)
        q = """SELECT hour, supply_index AS {p}sidx, borrow_index AS {p}bidx, price_usd AS {p}px,
                      liq_threshold AS {p}lt, liq_bonus AS {p}bonus,
                      borrowing AND active AND NOT frozen AND NOT paused AS {p}can_borrow
               FROM aave_reserve_hour WHERE chain = ? AND market = ? AND asset = ? ORDER BY hour"""
        a = con.execute(q.format(p="a_"), [chain, market, a_asset]).df()
        s = con.execute(q.format(p="s_"), [chain, market, s_asset]).df()
    finally:
        con.close()
    df = a.merge(s, on="hour")
    df = df[(df["a_px"] > 0) & (df["s_px"] > 0) & (df["a_lt"] > 0) & (df["s_lt"] > 0)].reset_index(drop=True)
    if len(df) < 24 * 30:
        raise BacktestError(f"{a_sym}/{s_sym} en {market}: menos de 30 días de datos")
    return {"chain": chain, "market": market, "asset": a_sym, "stable": s_sym}, df


def _stages(symbol: str, tf: str, since: pd.Timestamp) -> list[tuple[pd.Timestamp, float, pd.Series]]:
    """(hora de cierre, cierre, fila de classify) de cada vela cerrada desde `since`, calculada solo con
    las velas cerradas hasta ella."""
    cfg = TIMEFRAMES[tf]
    c = fetch_candles(symbol, cfg).df
    c.index = c.index.tz_localize(None) if c.index.tz is not None else c.index
    step = pd.Timedelta(days=7 if tf == "1w" else 1)
    first = max(cfg.min_bars(cfg.ma) + cfg.prior_window, int(c.index.searchsorted(since - 2 * step)))
    return [(c.index[t] + step, float(c["close"].iloc[t]), classify(c.iloc[:t + 1], cfg).iloc[-1])
            for t in range(first, len(c))]


def _band(row: pd.Series) -> tuple[float, float]:
    """(nivel de abajo, nivel de arriba) de la vela: el que invalida la etapa 2 y el que la confirma, al
    revés en la 4, y los bordes que confirman o invalidan la base o el techo en la 1 y la 3."""
    confirm, invalid = levels(row)
    if int(row["stage"]) == 2:
        return invalid, confirm
    if int(row["stage"]) == 4:
        return confirm, invalid
    return min(confirm, invalid), max(confirm, invalid)


def signals(symbol: str, since: pd.Timestamp) -> pd.DataFrame:
    """Una fila por cierre de vela (semanal o diaria) con las columnas que cambian en ese cierre:
    st_w (etapa semanal), nivel (modo de la regla de niveles) y st_d (etapa diaria)."""
    rows, prev, mode = [], None, None
    for t, close, r in _stages(symbol, "1w", since):
        if prev is not None:
            lo, hi = _band(prev)
            if lo == lo and close < lo:
                mode = "bajista"
            elif hi == hi and close > hi:
                mode = "alcista"
        mode = mode or TREND.get(int(r["stage"]))
        rows.append({"desde": t, "st_w": float(r["stage"]), "nivel": mode})
        prev = r
    rows += [{"desde": t, "st_d": float(r["stage"])} for t, _, r in _stages(symbol, "1d", since)]
    return pd.DataFrame(rows).sort_values("desde", kind="stable")


def attach_signals(df: pd.DataFrame, sig: pd.DataFrame) -> pd.DataFrame:
    for col in ("st_w", "nivel", "st_d"):
        s = sig.loc[sig[col].notna(), ["desde", col]]
        df = pd.merge_asof(df.sort_values("hour"), s, left_on="hour", right_on="desde", direction="backward")
        df = df.drop(columns="desde")
    return df


# --- simulación ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    rule: str
    capital: float = 11_500.0      # garantía al abrir, USD
    debt: float = 3_450.0          # deuda al abrir, USD (sale de Aave)
    manage: str = "nunca"          # nunca | defender
    defend_at: float = 1.3
    coll_bps: float = 30.0         # cambiar la garantía (comisión + deslizamiento)
    debt_bps: float = 5.0          # cambiar la deuda
    gas_usd: float = 0.05

    @property
    def name(self) -> str:
        return self.rule + (f" defender <{self.defend_at:g}" if self.manage == "defender" else "")


@dataclass
class Result:
    strategy: str
    start: str
    end: str
    days: float
    asset_change_pct: float
    equity0: float
    equity_end: float
    amortized: float               # (patrimonio final − inicial) / deuda inicial
    debt_end_usd: float
    switches: int
    switch_cost_usd: float
    time_in: dict                  # parte del tiempo en cada modo
    hf_min: float
    liquidations: int
    liquidation_loss_usd: float
    defenses: int
    added_usd: float


def targets(df: pd.DataFrame, rule: str) -> list[str | None]:
    """Modo que pide la regla en cada hora (None: quedarse como está)."""
    r = RULES[rule]
    if isinstance(r, str):
        return [r] * len(df)
    col, table = r
    vals = df[col].tolist()
    if table is None:
        return [v if isinstance(v, str) else None for v in vals]
    return [None if v is None or v != v else table.get(int(v)) for v in vals]


def simulate(df: pd.DataFrame, plan: Plan) -> Result:
    """Garantía y deuda pueden mezclar A y S: cada modo fija qué parte de cada una va en A. Al cambiar de
    modo se reequilibra solo lo que hace falta (Collateral Swap y Debt Swap sobre la parte que cambia).
    Liquidación aproximada: el liquidador se lleva garantía y cobra deuda en proporción a lo que vale cada
    token (en Aave elige uno; con los dos umbrales iguales, como cbBTC y USDC en Base, apenas cambia)."""
    K = ("a", "s")
    px = {k: df[f"{k}_px"].to_numpy() for k in K}
    sidx = {k: df[f"{k}_sidx"].to_numpy() for k in K}
    bidx = {k: df[f"{k}_bidx"].to_numpy() for k in K}
    lt = {k: df[f"{k}_lt"].to_numpy() for k in K}
    bonus = {k: df[f"{k}_bonus"].to_numpy() for k in K}
    can = {k: df[f"{k}_can_borrow"].to_numpy() for k in K}
    want_at = targets(df, plan.rule)

    def put(i, coll_a, coll_s, debt_a, debt_s):          # USD → unidades escaladas
        return ({"a": coll_a / (sidx["a"][i] * px["a"][i]), "s": coll_s / (sidx["s"][i] * px["s"][i])},
                {"a": debt_a / (bidx["a"][i] * px["a"][i]), "s": debt_s / (bidx["s"][i] * px["s"][i])})

    def value(i):
        c = {k: sc[k] * sidx[k][i] * px[k][i] for k in K}
        d = {k: sd[k] * bidx[k][i] * px[k][i] for k in K}
        return c, d

    mode = want_at[0] or "neutral"
    wc, wd = MODES[mode]
    sc, sd = put(0, plan.capital * wc, plan.capital * (1 - wc), plan.debt * wd, plan.debt * (1 - wd))
    hf0 = plan.capital * (wc * lt["a"][0] + (1 - wc) * lt["s"][0]) / plan.debt
    sell = plan.debt * wd * plan.debt_bps / 1e4             # vender la parte prestada en A
    gas, swcost = 2 * plan.gas_usd, sell                    # swcost: informativo (ya está en garantía y deuda)
    switches, hf_min, liqs, liq_loss, defs, added = 0, float("inf"), 0, 0.0, 0, 0.0
    hours = dict.fromkeys(MODES, 0)

    for i in range(len(df)):
        want = want_at[i] or mode
        if want != mode:
            nwc, nwd = MODES[want]
            c, d = value(i)
            C, D = c["a"] + c["s"], d["a"] + d["s"]
            more = {"a": nwd * D > d["a"] + 1e-9, "s": (1 - nwd) * D > d["s"] + 1e-9}
            if all(can[k][i] or not more[k] for k in K):
                cc = abs(nwc * C - c["a"]) * plan.coll_bps / 1e4      # Collateral Swap de la parte que cambia
                dc = abs(nwd * D - d["a"]) * plan.debt_bps / 1e4      # Debt Swap de la parte que cambia
                C, D = C - cc, D + dc
                sc, sd = put(i, nwc * C, (1 - nwc) * C, nwd * D, (1 - nwd) * D)
                gas += plan.gas_usd * ((cc > 0) + (dc > 0))
                swcost += cc + dc
                mode, wc, wd, switches = want, nwc, nwd, switches + 1
        c, d = value(i)
        coll_usd, debt_usd = c["a"] + c["s"], d["a"] + d["s"]
        hf = (c["a"] * lt["a"][i] + c["s"] * lt["s"][i]) / debt_usd if debt_usd > 1e-9 else float("inf")
        for _ in range(10):
            if hf >= 1 or debt_usd <= 1e-9:
                break
            cf = 1.0 if hf < FULL_BELOW_HF or debt_usd < SMALL_USD or coll_usd < SMALL_USD else CLOSE_FACTOR
            b = (c["a"] * bonus["a"][i] + c["s"] * bonus["s"][i]) / coll_usd
            seize = min(cf * debt_usd * (1 + b), coll_usd)
            repay = seize / (1 + b)
            for k in K:
                sc[k] *= 1 - seize / coll_usd
                sd[k] *= 1 - repay / debt_usd
            liqs, liq_loss = liqs + 1, liq_loss + seize - repay
            c, d = value(i)
            coll_usd, debt_usd = c["a"] + c["s"], d["a"] + d["s"]
            hf = (c["a"] * lt["a"][i] + c["s"] * lt["s"][i]) / debt_usd if debt_usd > 1e-9 else float("inf")
        if plan.manage == "defender" and hf < plan.defend_at and debt_usd > 0:
            pay = debt_usd - debt_usd * hf / hf0            # devolver hasta el HF inicial
            for k in K:
                sd[k] *= 1 - pay / debt_usd
            added, defs, gas = added + pay, defs + 1, gas + plan.gas_usd
            debt_usd, hf = debt_usd - pay, hf0
        hf_min = min(hf_min, hf)
        hours[mode] += 1

    n = len(df)
    c, d = value(n - 1)
    debt_usd = d["a"] + d["s"]
    eq0 = plan.capital - plan.debt
    eq1 = c["a"] + c["s"] - debt_usd - added - gas - sell
    h = df["hour"]
    return Result(plan.name, f"{h.iloc[0]:%Y-%m-%d}", f"{h.iloc[-1]:%Y-%m-%d}", round((n - 1) / 24, 1),
                  px["a"][-1] / px["a"][0] - 1, eq0, eq1, (eq1 - eq0) / plan.debt, debt_usd, switches,
                  swcost, {m: v / n for m, v in hours.items()}, hf_min, liqs, liq_loss, defs, added)


def summarize(rs: list[Result]) -> dict:
    s = pd.DataFrame([asdict(r) for r in rs])
    return {"strategy": rs[0].strategy, "ventanas": len(rs),
            "amort_mediana": float(s["amortized"].median()), "amort_p10": float(s["amortized"].quantile(0.1)),
            "amort_p90": float(s["amortized"].quantile(0.9)), "pct_positivas": float((s["amortized"] > 0).mean()),
            "pct_amortizado_100": float((s["amortized"] >= 1).mean()),
            "cambios_mediana": float(s["switches"].median()), "coste_mediana": float(s["switch_cost_usd"].median()),
            "liquidadas": int((s["liquidations"] > 0).sum()), "hf_min_peor": float(s["hf_min"].min()),
            "aportado_max": float(s["added_usd"].max())}


# --- presentación --------------------------------------------------------------------------------

def _p(x: float) -> str:
    return f"{x:+.1%}".replace(".", ",")


def _usd(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".") + " $"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etapas.aave_modos", description=__doc__.split("\n\n")[0])
    ap.add_argument("activo")
    ap.add_argument("estable")
    ap.add_argument("--red", required=True)
    ap.add_argument("--mercado")
    ap.add_argument("--senal", default=None, help="símbolo de Binance para la etapa (por defecto BTC si el activo es cbBTC/WBTC)")
    ap.add_argument("--capital", type=float, default=11_500.0)
    ap.add_argument("--deuda", type=float, default=3_450.0)
    ap.add_argument("--reglas", default="semanal,semanal-mixto,diaria,diaria-mixto,combinada,nivel,"
                                        "siempre-alcista,siempre-mixto,siempre-bajista,siempre-neutral")
    ap.add_argument("--gestion", default="nunca")
    ap.add_argument("--defensa", type=float, default=1.3)
    ap.add_argument("--coste-garantia", type=float, default=30.0)
    ap.add_argument("--coste-deuda", type=float, default=5.0)
    ap.add_argument("--ventana", default="", help="días de cada ventana, lista (vacío: solo el periodo completo)")
    ap.add_argument("--paso", type=int, default=7)
    ap.add_argument("--senales", help="fichero .pkl para guardar o reutilizar las señales (tardan unos 2 min)")
    a = ap.parse_args(argv)

    info, df = load(a.activo, a.estable, a.red, a.mercado)
    sym = a.senal or ("BTC" if "BTC" in info["asset"].upper() else "ETH" if "ETH" in info["asset"].upper() else info["asset"])
    if a.senales and os.path.exists(a.senales):
        sig = pd.read_pickle(a.senales)
    else:
        sig = signals(sym, df["hour"].iloc[0])
        if a.senales:
            sig.to_pickle(a.senales)
    df = attach_signals(df, sig)
    df = df[df["st_w"].notna() & df["st_d"].notna()].reset_index(drop=True)
    df["st_wd"] = [("alcista" if w == 2 else "bajista" if w == 4 else "mixto") if w == dd else "mixto"
                   for w, dd in zip(df["st_w"], df["st_d"])]
    plans = [Plan(r, a.capital, a.deuda, g, a.defensa, a.coste_garantia, a.coste_deuda)
             for r in a.reglas.split(",") for g in a.gestion.split(",")]

    print(f"Préstamo con cambio de modo · {info['asset']}/{info['stable']} · Aave V3 {info['market']} ({info['chain']})")
    print(f"Señales de {sym} (Binance, velas cerradas) · garantía {_usd(a.capital)} · deuda {_usd(a.deuda)} "
          f"· cambio de garantía {a.coste_garantia:g} pb, de deuda {a.coste_deuda:g} pb")
    print(f"Datos {df['hour'].iloc[0]:%Y-%m-%d} → {df['hour'].iloc[-1]:%Y-%m-%d}")
    ch = df.loc[df["st_w"].ne(df["st_w"].shift()), ["hour", "st_w", "a_px"]]
    print("Etapa semanal: " + " · ".join(f"{h:%d-%m-%y} E{int(s)} ({p:,.0f})" for h, s, p in ch.itertuples(index=False)))
    ch = df.loc[df["nivel"].ne(df["nivel"].shift()) & df["nivel"].notna(), ["hour", "nivel", "a_px"]]
    print("Niveles:       " + " · ".join(f"{h:%d-%m-%y} {m} ({p:,.0f})" for h, m, p in ch.itertuples(index=False)))
    d = df.loc[df["st_d"].ne(df["st_d"].shift()), "st_d"]
    print(f"Etapa diaria: {len(d)} cambios de etapa")

    print("\n## Periodo completo")
    print(f"{'estrategia':28} {'BTC':>7} {'patrim. final':>13} {'amortizado':>10} {'deuda final':>11} {'cambios':>7} "
          f"{'coste':>6} {'alc/mix/neu/baj':>16} {'HF mín':>6} {'liq':>3} {'aportado':>8}")
    for p in plans:
        r = simulate(df, p)
        print(f"{r.strategy:28} {_p(r.asset_change_pct):>7} {_usd(r.equity_end):>13} {_p(r.amortized):>10} "
              f"{_usd(r.debt_end_usd):>11} {r.switches:>7} {_usd(r.switch_cost_usd):>6} "
              f"{'/'.join(f'{r.time_in[m]:.0%}' for m in MODES):>16} {r.hf_min:>6.2f} {r.liquidations:>3} "
              f"{_usd(r.added_usd):>8}")

    for days in [int(x) for x in a.ventana.split(",") if x]:
        starts = pd.date_range(df["hour"].iloc[0], df["hour"].iloc[-1] - pd.Timedelta(days=days), freq=f"{a.paso}D")
        if not len(starts):
            continue
        print(f"\n## Ventanas de {days} días cada {a.paso} ({len(starts)} fechas de entrada)")
        print(f"{'estrategia':28} {'mediana':>8} {'peor 10%':>8} {'mejor 10%':>9} {'>0':>5} {'≥100%':>6} "
              f"{'cambios':>7} {'coste':>6} {'liq':>3} {'HF mín':>6} {'aportado':>8}")
        for p in plans:
            rs = [simulate(df[(df["hour"] >= s) & (df["hour"] <= s + pd.Timedelta(days=days))].reset_index(drop=True), p)
                  for s in starts]
            m = summarize(rs)
            print(f"{m['strategy']:28} {_p(m['amort_mediana']):>8} {_p(m['amort_p10']):>8} {_p(m['amort_p90']):>9} "
                  f"{m['pct_positivas']:>5.0%} {m['pct_amortizado_100']:>6.0%} {m['cambios_mediana']:>7.0f} "
                  f"{_usd(m['coste_mediana']):>6} {m['liquidadas']:>3} {m['hf_min_peor']:>6.2f} {_usd(m['aportado_max']):>8}")
    print("\nAmortizado = (patrimonio final − inicial) / deuda inicial: +100 % = la estrategia habría pagado el préstamo entero.")
    print("Patrimonio = garantía − deuda − aportado para defender − costes de cambio y gas. Lo prestado sale de Aave.")
    print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BacktestError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
