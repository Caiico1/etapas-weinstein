"""Backtest de estrategias de liquidez concentrada contra el histórico horario de MetaProDB
(D:\\MetaProDB\\metapro.duckdb, leído de la blockchain).

Cada hora se conoce el estado del pool al inicio de la hora (precio, tick, liquidez) y lo que cobró
cada unidad de liquidez en rango durante esa hora (fee_growth, el feeGrowthGlobal de la cadena).
Una posición con liquidez L cobra L × fee_growth en las horas en que está en rango: la mitad si
solo está en rango al principio o al final de la hora (el precio entró o salió dentro de ella).

Estrategias (combinables en rejilla):
  ancho     rango ±X % alrededor del precio de apertura (en escala logarítmica, ajustado a ticks)
  reajuste  nunca | fuera: recentrar el rango cuando el precio lleva `espera` horas fuera
  stop      cerrar a moneda estable si el valor cae X % por debajo del capital invertido
Costes: gas por apertura/reajuste/cierre y comisión del pool por el token que hay que cambiar.

    python -m etapas.backtest 0x88e6...5640 --desde 2026-07-19 --ancho 5,10,20 --reajuste nunca,fuera
    python -m etapas.backtest "WETH/USDC 0.05%" --ventana 30 --paso 7      # muchas fechas de entrada

Límites: el reparto de comisiones usa el feeGrowth GLOBAL del pool (lo que cobró la liquidez activa
en cada tick por el que pasó el precio): con rangos muy estrechos y horas muy movidas sobrestima.
No modela MEV, deslizamiento del swap de reajuste ni el retraso real al reajustar.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass

import pandas as pd

DB_PATH = os.environ.get("METAPRO_DB", r"D:\MetaProDB\metapro.duckdb")
TICK_BASE = 1.0001
MIN_TICK, MAX_TICK = -887272, 887272


class BacktestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Strategy:
    width: float                 # semiancho del rango: 0,10 = ±10 %
    rebalance: str = "nunca"     # nunca | fuera
    wait_hours: int = 0          # horas fuera de rango antes de reajustar
    stop: float | None = None    # 0,15 = cerrar si el valor cae un 15 % bajo el capital
    gas_usd: float = 5.0         # coste de cada transacción (abrir, reajustar, cerrar)

    @property
    def name(self) -> str:
        s = f"±{self.width:.0%} {self.rebalance}"
        if self.rebalance == "fuera" and self.wait_hours:
            s += f" {self.wait_hours}h"
        if self.stop:
            s += f" stop {self.stop:.0%}"
        return s


@dataclass
class Result:
    strategy: str
    start: str
    end: str
    days: float
    capital: float
    final: float                 # valor final (posición + comisiones o efectivo)
    pnl_pct: float
    fees_usd: float
    fees_apr: float              # comisiones anualizadas sobre el capital
    costs_usd: float             # gas + swaps
    vs_hodl_pct: float           # final frente a mantener lo depositado al principio
    vs_token0_pct: float         # frente a haber tenido todo en token0
    vs_token1_pct: float
    in_range_pct: float
    rebalances: int
    stopped: bool
    max_drawdown_pct: float


# --- datos -------------------------------------------------------------------------------------

def _connect(read_only: bool = True, wait_s: int = 120):
    import duckdb
    deadline = time.time() + wait_s
    while True:
        try:
            return duckdb.connect(DB_PATH, read_only=read_only)
        except duckdb.IOException:
            if time.time() > deadline:   # la actualización tiene la BD abierta unos segundos
                raise
            time.sleep(2)


def find_pool(con, key: str, chain: str | None = None) -> dict:
    q = ("SELECT p.chain, p.version, p.pool_id, p.label, p.fee, p.tick_spacing, t0.symbol, t1.symbol, "
         "t0.decimals, t1.decimals FROM pools p JOIN tokens t0 ON t0.chain = p.chain AND t0.address = p.token0 "
         "JOIN tokens t1 ON t1.chain = p.chain AND t1.address = p.token1 "
         "WHERE (p.pool_id = lower(?) OR p.label ILIKE ?)" + (" AND p.chain = ?" if chain else ""))
    rows = con.execute(q, [key, f"%{key}%"] + ([chain] if chain else [])).fetchall()
    if not rows:
        raise BacktestError(f"no hay ningún pool «{key}» en MetaProDB")
    if len(rows) > 1:
        opts = "\n  ".join(f"{r[0]} {r[1]} {r[2]} {r[3]}" for r in rows)
        raise BacktestError(f"«{key}» coincide con varios pools; usa la dirección o --red:\n  {opts}")
    keys = ("chain", "version", "pool_id", "label", "fee", "tick_spacing", "symbol0", "symbol1", "dec0", "dec1")
    return dict(zip(keys, rows[0]))


def load_hours(key: str, chain: str | None = None, start: str | None = None, end: str | None = None) -> tuple[dict, pd.DataFrame]:
    con = _connect()
    try:
        pool = find_pool(con, key, chain)
        df = con.execute("""
            SELECT v.hour, v.tick, h.sqrt_price, v.usd0, v.usd1, v.fee_growth0, v.fee_growth1, v.lp_fee
            FROM pool_hour_v v JOIN pool_hour h USING (chain, pool_id, hour)
            WHERE v.chain = ? AND v.pool_id = ? AND (? IS NULL OR v.hour >= ?) AND (? IS NULL OR v.hour <= ?)
            ORDER BY v.hour""", [pool["chain"], pool["pool_id"], start, start, end, end]).df()
    finally:
        con.close()
    df = df.dropna(subset=["usd0", "usd1"]).reset_index(drop=True)
    # Mientras se carga el histórico puede haber huecos: se usa el tramo continuo más reciente.
    gaps = df.index[df["hour"].diff() > pd.Timedelta(hours=6)]
    if len(gaps):
        cut = gaps[-1]
        pool["aviso"] = (f"hay huecos en el histórico; se usa el tramo continuo desde "
                         f"{df['hour'].iloc[cut]:%Y-%m-%d %H:00} (descartadas {cut} horas anteriores)")
        df = df.iloc[cut:]
    if len(df) < 48:
        raise BacktestError(f"{pool['label']}: menos de 48 horas con precio en USD en ese periodo")
    df[["fee_growth0", "fee_growth1"]] = df[["fee_growth0", "fee_growth1"]].fillna(0.0)
    return pool, df.reset_index(drop=True)


# --- matemática de Uniswap v3 en unidades del pool (whitepaper, sección 6) ---------------------

def _amounts(sp: float, sa: float, sb: float) -> tuple[float, float]:
    """(token0, token1) en unidades mínimas por unidad de liquidez, con √precio sp y rango [sa, sb]."""
    if sp <= sa:
        return 1 / sa - 1 / sb, 0.0
    if sp >= sb:
        return 0.0, sb - sa
    return 1 / sp - 1 / sb, sp - sa


def _range(tick: int, width: float, spacing: int) -> tuple[int, int]:
    half = math.log(1 + width) / math.log(TICK_BASE)
    lo = max(math.floor((tick - half) / spacing) * spacing, math.ceil(MIN_TICK / spacing) * spacing)
    hi = min(math.ceil((tick + half) / spacing) * spacing, math.floor(MAX_TICK / spacing) * spacing)
    return lo, max(hi, lo + spacing)


def _sqrt(tick: int) -> float:
    return TICK_BASE ** (tick / 2)


# --- simulación ---------------------------------------------------------------------------------

def simulate(pool: dict, df: pd.DataFrame, st: Strategy, capital: float = 10_000.0) -> Result:
    d0, d1, fee = 10 ** pool["dec0"], 10 ** pool["dec1"], pool["fee"] / 1e6
    ticks, sps = df["tick"].to_numpy(), df["sqrt_price"].to_numpy()
    u0s, u1s = df["usd0"].to_numpy(), df["usd1"].to_numpy()
    g0s, g1s = df["fee_growth0"].to_numpy(), df["fee_growth1"].to_numpy()
    n = len(df)

    def usd(a0: float, a1: float, i: int) -> float:
        return a0 / d0 * u0s[i] + a1 / d1 * u1s[i]

    def open_at(i: int, value: float) -> tuple[int, int, float, float]:
        """Abre el rango alrededor del tick i con `value` dólares. Devuelve (tl, tu, L, coste swap)."""
        tl, tu = _range(int(ticks[i]), st.width, pool["tick_spacing"])
        a0, a1 = _amounts(sps[i], _sqrt(tl), _sqrt(tu))
        per_l = usd(a0, a1, i)
        share0 = (a0 / d0 * u0s[i]) / per_l if per_l else 0.5
        return tl, tu, value / per_l, share0

    stable0, stable1 = pool["symbol0"] in _STABLE, pool["symbol1"] in _STABLE
    costs = st.gas_usd
    value = capital - costs
    # Se parte de dólares: comprar la parte no estable que pide el rango cuesta la comisión del pool
    # (si ninguno de los dos es estable, se supone que se compra la mitad del capital).
    _, _, _, share0 = open_at(0, value)
    swap = value * fee * ((1 - share0) if stable0 else share0 if stable1 else 0.5)
    costs += swap
    tl, tu, L, share0 = open_at(0, value - swap)
    a0_0, a1_0 = (x * L for x in _amounts(sps[0], _sqrt(tl), _sqrt(tu)))
    hodl0, hodl1 = a0_0, a1_0                      # lo depositado, para comparar con mantener
    f0 = f1 = 0.0
    fees_usd = 0.0
    out_hours, rebalances, in_range_h = 0, 0, 0.0
    stopped, cash = False, None
    peak, max_dd = capital, 0.0

    for i in range(n - 1):
        if cash is not None:
            break
        inside_now = bool(tl <= ticks[i] < tu)          # bool(): en numpy, True + True = True
        inside_next = bool(tl <= ticks[i + 1] < tu)
        part = (int(inside_now) + int(inside_next)) / 2
        in_range_h += part
        df0, df1 = L * g0s[i] * part, L * g1s[i] * part
        f0 += df0
        f1 += df1
        fees_usd += usd(df0, df1, i + 1)

        j = i + 1
        a0, a1 = (x * L for x in _amounts(sps[j], _sqrt(tl), _sqrt(tu)))
        val = usd(a0 + f0, a1 + f1, j)
        peak = max(peak, val)
        max_dd = max(max_dd, 1 - val / peak)

        if st.stop and val < capital * (1 - st.stop):
            # Cierre a moneda estable: se vende lo que no es estable pagando la comisión del pool.
            nonstable = usd(0, a1 + f1, j) if stable0 else usd(a0 + f0, 0, j) if stable1 else val
            c = st.gas_usd + nonstable * fee
            costs += c
            cash = val - c
            stopped = True
            break

        out_hours = 0 if inside_next else out_hours + 1
        if st.rebalance == "fuera" and out_hours > st.wait_hours and j < n - 1:
            ntl, ntu, nL, nshare0 = open_at(j, 1.0)
            cur0 = usd(a0 + f0, 0, j) / val if val else 0.5
            c = st.gas_usd + val * abs(cur0 - nshare0) * fee
            costs += c
            tl, tu, L, _ = open_at(j, val - c)
            f0 = f1 = 0.0
            out_hours = 0
            rebalances += 1

    last = n - 1
    if cash is None:
        a0, a1 = (x * L for x in _amounts(sps[last], _sqrt(tl), _sqrt(tu)))
        final = usd(a0 + f0, a1 + f1, last)
    else:
        final = cash
    hours = (df["hour"].iloc[last] - df["hour"].iloc[0]).total_seconds() / 3600
    hodl = usd(hodl0, hodl1, last) + (capital - usd(hodl0, hodl1, 0))      # mismos costes de entrada
    only0 = capital * u0s[last] / u0s[0]
    only1 = capital * u1s[last] / u1s[0]
    return Result(st.name, str(df["hour"].iloc[0]), str(df["hour"].iloc[last]), round(hours / 24, 1),
                  capital, final, final / capital - 1, fees_usd, fees_usd / capital * 8760 / max(hours, 1),
                  costs, final / hodl - 1, final / only0 - 1, final / only1 - 1,
                  in_range_h / max(n - 1, 1), rebalances, stopped, max_dd)


_STABLE = {"USDC", "USDT", "DAI", "USDe", "AUSD", "USDS", "PYUSD", "FRAX", "GHO", "USDC.e", "USDbC", "USDT0", "RLUSD", "USD1"}


def windows(pool: dict, df: pd.DataFrame, st: Strategy, days: int, step: int, capital: float) -> list[Result]:
    """La misma estrategia abierta en muchas fechas: cada `step` días, durante `days` días."""
    out = []
    hours = df["hour"]
    t = hours.iloc[0]
    while t + pd.Timedelta(days=days) <= hours.iloc[-1]:
        part = df[(hours >= t) & (hours <= t + pd.Timedelta(days=days))].reset_index(drop=True)
        if len(part) >= days * 24 * 0.9:
            out.append(simulate(pool, part, st, capital))
        t += pd.Timedelta(days=step)
    return out


def summarize(rs: list[Result]) -> dict:
    s = pd.DataFrame([asdict(r) for r in rs])
    return {"strategy": rs[0].strategy, "ventanas": len(rs),
            "pnl_mediana": s["pnl_pct"].median(), "pnl_p10": s["pnl_pct"].quantile(0.1),
            "pct_positivas": (s["pnl_pct"] > 0).mean(), "vs_hodl_mediana": s["vs_hodl_pct"].median(),
            "fees_apr_mediana": s["fees_apr"].median(), "en_rango_mediana": s["in_range_pct"].median(),
            "reajustes_media": s["rebalances"].mean(), "stops": int(s["stopped"].sum()),
            "max_dd_mediana": s["max_drawdown_pct"].median()}


# --- línea de órdenes ---------------------------------------------------------------------------

def _pct(x: float) -> str:
    return f"{x:+.2%}".replace(".", ",")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etapas.backtest", description=__doc__.split("\n\n")[0])
    ap.add_argument("pool", help="dirección/poolId o parte de la etiqueta (p. ej. «WETH/USDC 0.05%%»)")
    ap.add_argument("--red", help="ethereum | base | arbitrum (si la etiqueta es ambigua)")
    ap.add_argument("--desde"), ap.add_argument("--hasta")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--ancho", default="10", help="semiancho del rango en %%, lista: 5,10,20")
    ap.add_argument("--reajuste", default="nunca", help="nunca | fuera, lista: nunca,fuera")
    ap.add_argument("--espera", default="0", help="horas fuera de rango antes de reajustar, lista: 0,24")
    ap.add_argument("--stop", default="", help="caída en %% para cerrar, lista: 0,15 (0 = sin stop)")
    ap.add_argument("--gas", type=float, default=None, help="dólares por transacción (por defecto: 5 en Ethereum, 0,05 en L2)")
    ap.add_argument("--ventana", type=int, help="días de cada prueba: abrir en muchas fechas")
    ap.add_argument("--paso", type=int, default=7, help="días entre fechas de entrada (con --ventana)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        pool, df = load_hours(a.pool, a.red, a.desde, a.hasta)
    except BacktestError as e:
        print(e, file=sys.stderr)
        return 2
    gas = a.gas if a.gas is not None else (5.0 if pool["chain"] == "ethereum" else 0.05)
    grid = [Strategy(float(w) / 100, r, int(h), (float(s) / 100 or None) if s else None, gas)
            for w, r, h, s in itertools.product(a.ancho.split(","), a.reajuste.split(","),
                                                a.espera.split(","), (a.stop.split(",") if a.stop else [""]))
            if not (r == "nunca" and h != a.espera.split(",")[0])]
    head = f"{pool['label']} · {pool['chain']} {pool['version']} · {pool['pool_id']}"
    if pool.get("aviso") and not a.json:
        head += f"\nAviso: {pool['aviso']}"
    if a.ventana:
        rows = [summarize(rs) for st in grid if (rs := windows(pool, df, st, a.ventana, a.paso, a.capital))]
        if a.json:
            print(json.dumps({"pool": pool, "ventana_dias": a.ventana, "resultados": rows}, default=str, indent=1))
            return 0
        print(f"{head}\nVentanas de {a.ventana} días cada {a.paso} días · {df['hour'].iloc[0]:%Y-%m-%d} → {df['hour'].iloc[-1]:%Y-%m-%d}\n")
        print(f"{'estrategia':<24}{'n':>4}{'mediana':>10}{'peor 10%':>10}{'>0':>6}{'vs HODL':>10}{'APR com.':>10}{'en rango':>10}{'reaj.':>7}{'stops':>6}")
        for r in rows:
            print(f"{r['strategy']:<24}{r['ventanas']:>4}{_pct(r['pnl_mediana']):>10}{_pct(r['pnl_p10']):>10}"
                  f"{r['pct_positivas']:>6.0%}{_pct(r['vs_hodl_mediana']):>10}{r['fees_apr_mediana']:>10.1%}"
                  f"{r['en_rango_mediana']:>10.0%}{r['reajustes_media']:>7.1f}{r['stops']:>6}")
        return 0
    res = [simulate(pool, df, st, a.capital) for st in grid]
    if a.json:
        print(json.dumps({"pool": pool, "resultados": [asdict(r) for r in res]}, default=str, indent=1))
        return 0
    print(f"{head}\n{res[0].start} → {res[0].end} ({res[0].days} días) · capital {a.capital:,.0f} $ · gas {gas} $/tx\n")
    print(f"{'estrategia':<24}{'final $':>10}{'resultado':>10}{'comisiones':>11}{'APR com.':>9}{'costes':>8}"
          f"{'vs HODL':>9}{'vs ' + pool['symbol0']:>10}{'vs ' + pool['symbol1']:>10}{'en rango':>9}{'reaj.':>6}{'máx. caída':>11}")
    for r in res:
        print(f"{r.strategy:<24}{r.final:>10,.0f}{_pct(r.pnl_pct):>10}{r.fees_usd:>11,.0f}{r.fees_apr:>9.1%}{r.costs_usd:>8,.0f}"
              f"{_pct(r.vs_hodl_pct):>9}{_pct(r.vs_token0_pct):>10}{_pct(r.vs_token1_pct):>10}{r.in_range_pct:>9.0%}"
              f"{r.rebalances:>6}{r.max_drawdown_pct:>11.1%}" + ("  (stop)" if r.stopped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
