"""Backtest de estrategias de Aave V3 contra el histórico horario de MetaProDB (tablas aave_*,
leídas de la blockchain): préstamo con garantía y liquidez cubierta.

Préstamo con garantía: depositas `capital` en un token (la garantía, p. ej. WETH) y pides prestado
otro (la deuda, p. ej. USDC) hasta el factor de salud (HF) elegido. Cada hora:
  - la garantía crece con el índice real de depósito y la deuda con el índice real de préstamo
    (lo que se cobró y se pagó de verdad, aunque el tipo cambiara dentro de la hora);
  - las dos se valoran con el precio del oráculo de Aave, el que decide las liquidaciones;
  - HF = garantía × umbral de liquidación vigente en esa hora / deuda;
  - si HF < 1, liquidación como en Aave V3 (≥ 3.3): el liquidador paga el 50 % de la deuda (toda si
    HF < 0,95 o si la deuda o la garantía valen menos de 2.000 $) y se lleva garantía por ese valor
    más el bonus de liquidación. Se repite mientras HF siga < 1.
Gestión: nunca (no tocar) | defender (si HF baja de --defensa, devolver deuda con dinero de fuera
hasta volver al HF inicial; se anota cuánto hizo falta aportar).
Lo prestado: se supone depositado en Aave en el mismo mercado (cobra el APY de depósito de la deuda);
el «coste neto» es lo pagado por el préstamo menos lo cobrado por ese depósito.

Liquidez cubierta (como estrategia.py): con capital C en la estable de un pool activo/estable, se
deposita D de la estable en Aave, se pide prestado justo el activo que pide el rango y el pool recibe
ese activo más el resto de la estable. Con s = parte del activo en la posición y l = umbral / HF:
V = C / (s/l + 1 − s), D = s·V / l, deuda = s·V. El HF solo depende de la garantía (estable) y de la
deuda (activo): hay liquidación si el activo sube. El pool se simula con backtest.simulate (comisiones
reales, reajuste opcional; sin stop: vender el activo dejaría la deuda al descubierto). Se compara
con dejar C en la estable en Aave y con el mismo pool sin cubrir.

Medida clave: sin liquidación, HF(t) = HF inicial × r(t), con r(t) independiente del HF elegido.
Así que el HF inicial que habría aguantado una ventana es 1 / mín r(t) («HF necesario»).

    python -m etapas.aave_backtest WETH USDC --red base --hf 1.5,1.8,2.2 --ventana 60 --paso 7
    python -m etapas.aave_backtest cbBTC USDC --red base --hf 1.8 --gestion nunca,defender --desde 2025-01-01
    python -m etapas.aave_backtest cubierta "WETH/USDC 0.05%" --red base --ancho 10,20 --hf 2,2.5 --ventana 60

Límites: precio y HF vistos cada hora (una mecha dentro de la hora no se ve); no modela el
cambio de tipo que provocaría tu propio préstamo ni la liquidez disponible para retirar.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass

import pandas as pd

from . import backtest as B
from .backtest import BacktestError, _connect, _pct, windows

ALIASES = {"ETH": "WETH"}
CLOSE_FACTOR, FULL_BELOW_HF, SMALL_USD = 0.5, 0.95, 2_000.0
YEAR_H = 24 * 365


class CannotOpen(BacktestError):
    """En esa hora Aave no dejaba abrir el préstamo (LTV 0, reserva congelada o HF por debajo del LTV)."""


@dataclass(frozen=True)
class Loan:
    hf: float                      # factor de salud al abrir
    manage: str = "nunca"          # nunca | defender
    defend_at: float = 1.3         # HF que dispara la defensa
    gas_usd: float = 0.05          # por transacción (depositar, pedir, devolver, retirar)

    @property
    def name(self) -> str:
        return f"HF {self.hf:.2f} {self.manage}" + (f" <{self.defend_at:.2f}" if self.manage == "defender" else "")


@dataclass
class LoanResult:
    strategy: str
    start: str
    end: str
    days: float
    capital: float
    debt_usd: float                # deuda al abrir
    ltv_open: float                # deuda / garantía al abrir
    hf_min: float
    hf_needed: float               # HF inicial que habría evitado la liquidación en esta ventana
    hours_below_12: int            # horas con HF < 1,2
    liquidations: int
    liquidated_usd: float          # deuda pagada por liquidadores
    liquidation_loss_usd: float    # bonus que se llevaron
    defenses: int
    added_usd: float               # dinero aportado para defender
    interest_usd: float            # intereses pagados
    borrow_apr: float              # intereses / deuda media, anualizado
    carry_usd: float               # intereses cobrados por lo prestado depositado en Aave
    collateral_yield_usd: float    # intereses cobrados por la garantía
    net_cost_apr: float            # (pagado − cobrado) / deuda media, anualizado
    final_usd: float               # garantía − deuda + lo prestado − aportado − gas
    hold_usd: float                # solo la garantía depositada, sin préstamo
    vs_hold_pct: float             # (final − mantener) / capital
    gas_usd: float


# --- datos -------------------------------------------------------------------------------------

def _reserve(con, chain: str, market: str, symbol: str) -> tuple[str, str]:
    symbol = ALIASES.get(symbol.upper(), symbol)
    rows = con.execute("SELECT asset, symbol FROM aave_reserves WHERE chain = ? AND market = ? AND lower(symbol) = lower(?)",
                       [chain, market, symbol]).fetchall()
    if len(rows) != 1:
        opts = ", ".join(s for (s,) in con.execute(
            "SELECT symbol FROM aave_reserves WHERE chain = ? AND market = ? ORDER BY symbol", [chain, market]).fetchall())
        raise BacktestError(f"«{symbol}» no está en el mercado {market} de {chain}. Hay: {opts}")
    return rows[0]


def load_pair(collateral: str, debt: str, chain: str, market: str | None = None,
              start: str | None = None, end: str | None = None) -> tuple[dict, pd.DataFrame]:
    market = market or chain
    con = _connect()
    try:
        c_asset, c_sym = _reserve(con, chain, market, collateral)
        d_asset, d_sym = _reserve(con, chain, market, debt)
        df = con.execute("""
            SELECT c.hour, c.supply_index AS c_index, c.price_usd AS c_price, c.ltv, c.liq_threshold AS lt,
                   c.liq_bonus AS bonus, d.borrow_index AS d_index, d.supply_index AS d_supply_index,
                   d.price_usd AS d_price, d.borrowing AND d.active AND NOT d.frozen AND NOT d.paused AS can_borrow
            FROM aave_reserve_hour c JOIN aave_reserve_hour d USING (chain, market, hour)
            WHERE c.chain = ? AND c.market = ? AND c.asset = ? AND d.asset = ?
              AND (? IS NULL OR c.hour >= ?) AND (? IS NULL OR c.hour <= ?)
            ORDER BY c.hour""", [chain, market, c_asset, d_asset, start, start, end, end]).df()
    finally:
        con.close()
    df = df[(df["c_price"] > 0) & (df["d_price"] > 0) & (df["lt"] > 0)].reset_index(drop=True)
    if len(df) < 48:
        raise BacktestError(f"{c_sym}/{d_sym} en {market}: menos de 48 horas en las que {c_sym} sirva de garantía")
    return {"chain": chain, "market": market, "collateral": c_sym, "debt": d_sym}, df


# --- simulación ----------------------------------------------------------------------------------

def hf_needed(df: pd.DataFrame) -> float:
    """HF inicial mínimo para no ser liquidado en el tramo (sin gestión): 1 / mín r(t)."""
    coll = df["c_index"] * df["c_price"] * df["lt"]
    debt = df["d_index"] * df["d_price"]
    r = (coll / debt) / (coll.iloc[0] / debt.iloc[0])
    return float(1 / r.min())


def simulate(df: pd.DataFrame, loan: Loan, capital: float = 10_000.0, keep_proceeds: bool = True) -> LoanResult:
    """`capital` = garantía depositada. keep_proceeds: lo prestado se deja depositado en Aave y cuenta
    en el resultado; si es False (liquidez cubierta) sale del préstamo y el resultado es garantía − deuda."""
    c_idx, c_px, lt, bonus = (df[k].to_numpy() for k in ("c_index", "c_price", "lt", "bonus"))
    d_idx, d_sidx, d_px = (df[k].to_numpy() for k in ("d_index", "d_supply_index", "d_price"))
    ltv0 = float(df["ltv"].iloc[0])
    if ltv0 <= 0 or not bool(df["can_borrow"].iloc[0]):
        raise CannotOpen(f"el {df['hour'].iloc[0]:%Y-%m-%d %H:00} Aave no dejaba abrir este préstamo "
                         f"(LTV {ltv0:.0%} o deuda congelada)")
    sc = capital / c_px[0] / c_idx[0]                       # garantía en unidades «escaladas»
    debt0 = capital * lt[0] / loan.hf                       # deuda al abrir, en USD
    if debt0 > capital * ltv0:
        raise CannotOpen(f"HF {loan.hf:.2f} pide más del LTV permitido al abrir ({ltv0:.0%}); "
                            f"el HF mínimo para abrir es {lt[0] / ltv0:.2f}")
    sd = debt0 / d_px[0] / d_idx[0]                         # deuda escalada
    sp = sd * d_idx[0] / d_sidx[0] if keep_proceeds else 0.0   # lo prestado, depositado en Aave (escalado)
    gas = 4 * loan.gas_usd
    hf_min, below, liqs, liq_usd, liq_loss, defs, added = float("inf"), 0, 0, 0.0, 0.0, 0, 0.0
    interest = carry = coll_yield = debt_hours = 0.0

    for i in range(len(df)):
        if i:
            interest += sd * (d_idx[i] - d_idx[i - 1]) * d_px[i]
            coll_yield += sc * (c_idx[i] - c_idx[i - 1]) * c_px[i]
            # solo lo prestado que sigue respaldado por deuda: tras una liquidación o una defensa, el resto
            # ya no forma parte del préstamo (sigue en «final», pero no rebaja su coste)
            matched = min(sp * d_sidx[i - 1], sd * d_idx[i - 1])
            carry += matched * (d_sidx[i] / d_sidx[i - 1] - 1) * d_px[i]
        coll_usd, debt_usd = sc * c_idx[i] * c_px[i], sd * d_idx[i] * d_px[i]
        hf = coll_usd * lt[i] / debt_usd if debt_usd > 0 else float("inf")
        for _ in range(10):                                 # liquidaciones en esa hora
            if hf >= 1 or debt_usd <= 0:
                break
            cf = 1.0 if hf < FULL_BELOW_HF or debt_usd < SMALL_USD or coll_usd < SMALL_USD else CLOSE_FACTOR
            repay = cf * debt_usd
            seize = min(repay * (1 + bonus[i]), coll_usd)
            repay = seize / (1 + bonus[i])
            sc -= seize / (c_idx[i] * c_px[i])
            sd -= repay / (d_idx[i] * d_px[i])
            liqs, liq_usd, liq_loss = liqs + 1, liq_usd + repay, liq_loss + seize - repay
            coll_usd, debt_usd = sc * c_idx[i] * c_px[i], sd * d_idx[i] * d_px[i]
            hf = coll_usd * lt[i] / debt_usd if debt_usd > 1e-9 else float("inf")
        if loan.manage == "defender" and hf < loan.defend_at and debt_usd > 0:
            target = coll_usd * lt[i] / loan.hf             # deuda que devuelve el HF inicial
            pay = debt_usd - target
            sd -= pay / (d_idx[i] * d_px[i])
            added, defs, gas = added + pay, defs + 1, gas + loan.gas_usd
            debt_usd = target
            hf = loan.hf
        hf_min = min(hf_min, hf)
        below += hf < 1.2
        debt_hours += debt_usd

    n = len(df) - 1
    years = n / YEAR_H
    avg_debt = debt_hours / len(df)
    final = sc * c_idx[-1] * c_px[-1] - sd * d_idx[-1] * d_px[-1] + sp * d_sidx[-1] * d_px[-1] - added - gas
    hold = capital / c_px[0] / c_idx[0] * c_idx[-1] * c_px[-1]
    apr = (lambda x: x / avg_debt / years if avg_debt > 0 and years > 0 else 0.0)
    h = df["hour"]
    return LoanResult(loan.name, f"{h.iloc[0]:%Y-%m-%d %H:00}", f"{h.iloc[-1]:%Y-%m-%d %H:00}", round(n / 24, 1),
                      capital, debt0, debt0 / capital, hf_min, hf_needed(df), int(below), liqs, liq_usd, liq_loss,
                      defs, added, interest, apr(interest), carry, coll_yield, apr(interest - carry), final, hold,
                      (final - hold) / capital, gas)


def summarize(rs: list[LoanResult | None], loan: Loan) -> dict | None:
    blocked = sum(r is None for r in rs)
    rs = [r for r in rs if r is not None]
    if not rs:
        return None
    s = pd.DataFrame([asdict(r) for r in rs])
    return {"strategy": loan.name, "ventanas": len(rs), "sin_abrir": blocked,
            "liquidadas": int((s["liquidations"] > 0).sum()), "pct_liquidadas": float((s["liquidations"] > 0).mean()),
            "hf_min_mediana": float(s["hf_min"].median()), "hf_min_peor": float(s["hf_min"].min()),
            "hf_necesario_max": float(s["hf_needed"].max()), "hf_necesario_p90": float(s["hf_needed"].quantile(0.9)),
            "coste_apr_mediana": float(s["borrow_apr"].median()), "coste_apr_max": float(s["borrow_apr"].max()),
            "coste_neto_apr_mediana": float(s["net_cost_apr"].median()),
            "perdida_liquidacion_max": float(s["liquidation_loss_usd"].max()),
            "vs_mantener_mediana": float(s["vs_hold_pct"].median()), "vs_mantener_p10": float(s["vs_hold_pct"].quantile(0.1)),
            "defensas_media": float(s["defenses"].mean()), "aportado_max": float(s["added_usd"].max())}


# --- liquidez cubierta ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Covered:
    width: float                   # semiancho del rango del pool: 0,10 = ±10 %
    hf: float                      # factor de salud al abrir (l = umbral / HF)
    manage: str = "nunca"          # nunca | defender (con dinero de fuera, como en el préstamo)
    defend_at: float = 1.3
    rebalance: str = "nunca"       # el pool: nunca | fuera
    wait_hours: int = 0
    gas_usd: float = 0.05

    @property
    def name(self) -> str:
        s = f"±{self.width:.0%} {self.rebalance} · HF {self.hf:.2f}"
        return s + (f" def<{self.defend_at:.2f}" if self.manage == "defender" else "")


@dataclass
class CoveredResult:
    strategy: str
    start: str
    end: str
    days: float
    capital: float
    lp_value: float                # V: tamaño de la posición en el pool
    deposit: float                 # D: estable depositada en Aave como garantía
    debt_usd: float                # activo prestado, en USD al abrir
    final_usd: float               # pool + garantía − deuda − aportado − gas
    pnl_pct: float
    fees_usd: float                # comisiones del pool
    fees_apr: float                # sobre el capital
    interest_usd: float            # intereses del préstamo
    collateral_yield_usd: float    # lo que cobró la garantía
    hf_min: float
    hf_needed: float
    liquidations: int
    liquidation_loss_usd: float
    defenses: int
    added_usd: float
    in_range_pct: float
    rebalances: int
    vs_stable_pct: float           # frente a dejar todo el capital en la estable en Aave
    vs_unhedged_pct: float         # frente al mismo pool sin cubrir, con todo el capital


def load_covered(key: str, chain: str | None = None, market: str | None = None,
                 start: str | None = None, end: str | None = None) -> tuple[dict, pd.DataFrame]:
    """El pool (backtest.load_hours) y, en las mismas horas, la estable como garantía y el activo como deuda."""
    pool, pdf = B.load_hours(key, chain, start, end)
    s0, s1 = pool["symbol0"] in B._STABLE, pool["symbol1"] in B._STABLE
    if s0 == s1:
        raise BacktestError(f"{pool['label']}: la liquidez cubierta necesita un par activo/estable")
    stable, asset = (pool["symbol0"], pool["symbol1"]) if s0 else (pool["symbol1"], pool["symbol0"])
    info, adf = load_pair(stable, asset, pool["chain"], market, start, end)
    df = pdf.merge(adf, on="hour", how="inner").reset_index(drop=True)
    if len(df) < 48:
        raise BacktestError(f"{pool['label']}: menos de 48 horas con datos del pool y de Aave a la vez")
    return {**pool, "asset_is_token0": s1, "market": info["market"], "collateral": info["collateral"],
            "debt": info["debt"]}, df


def asset_share(pool: dict, df: pd.DataFrame, width: float) -> float:
    """Parte del valor de la posición que va en el activo al abrir el rango (como backtest.simulate)."""
    tl, tu = B._range(int(df["tick"].iloc[0]), width, pool["tick_spacing"])
    a0, a1 = B._amounts(df["sqrt_price"].iloc[0], B._sqrt(tl), B._sqrt(tu))
    v0, v1 = a0 / 10 ** pool["dec0"] * df["usd0"].iloc[0], a1 / 10 ** pool["dec1"] * df["usd1"].iloc[0]
    return (v0 if pool["asset_is_token0"] else v1) / (v0 + v1)


def simulate_covered(pool: dict, df: pd.DataFrame, cv: Covered, capital: float = 10_000.0) -> CoveredResult:
    lt0, ltv0 = float(df["lt"].iloc[0]), float(df["ltv"].iloc[0])
    used = lt0 / cv.hf                                      # LTV usado
    s = asset_share(pool, df, cv.width)
    value = capital / (s / used + 1 - s)
    deposit = s * value / used
    loan = simulate(df, Loan(cv.hf, cv.manage, cv.defend_at, cv.gas_usd), deposit, keep_proceeds=False)
    st = B.Strategy(cv.width, cv.rebalance, cv.wait_hours, None, cv.gas_usd)
    lp = B.simulate(pool, df, st, value)
    final = lp.final + loan.final_usd
    stable_only = capital * df["c_index"].iloc[-1] / df["c_index"].iloc[0]
    unhedged = B.simulate(pool, df, st, capital).final
    years = max(loan.days, 1 / 24) / 365
    return CoveredResult(cv.name, loan.start, loan.end, loan.days, capital, value, deposit, loan.debt_usd, final,
                         final / capital - 1, lp.fees_usd, lp.fees_usd / capital / years, loan.interest_usd,
                         loan.collateral_yield_usd, loan.hf_min, loan.hf_needed, loan.liquidations,
                         loan.liquidation_loss_usd, loan.defenses, loan.added_usd, lp.in_range_pct, lp.rebalances,
                         (final - stable_only) / capital, (final - unhedged) / capital)


def summarize_covered(rs: list[CoveredResult | None], cv: Covered) -> dict | None:
    blocked = sum(r is None for r in rs)
    rs = [r for r in rs if r is not None]
    if not rs:
        return None
    s = pd.DataFrame([asdict(r) for r in rs])
    years = s["days"] / 365
    return {"strategy": cv.name, "ventanas": len(rs), "sin_abrir": blocked,
            "pnl_mediana": float(s["pnl_pct"].median()), "pnl_p10": float(s["pnl_pct"].quantile(0.1)),
            "pct_positivas": float((s["pnl_pct"] > 0).mean()),
            "vs_estable_mediana": float(s["vs_stable_pct"].median()), "vs_estable_p10": float(s["vs_stable_pct"].quantile(0.1)),
            "vs_sin_cubrir_mediana": float(s["vs_unhedged_pct"].median()),
            "fees_apr_mediana": float(s["fees_apr"].median()),
            "aave_neto_apr_mediana": float(((s["collateral_yield_usd"] - s["interest_usd"]) / s["capital"] / years).median()),
            "liquidadas": int((s["liquidations"] > 0).sum()), "hf_min_peor": float(s["hf_min"].min()),
            "hf_necesario_max": float(s["hf_needed"].max()), "aportado_max": float(s["added_usd"].max()),
            "en_rango_mediana": float(s["in_range_pct"].median()), "pool_pct_capital": float((s["lp_value"] / s["capital"]).median())}


def _try_covered(pool: dict, df: pd.DataFrame, cv: Covered, capital: float) -> CoveredResult | None:
    try:
        return simulate_covered(pool, df, cv, capital)
    except CannotOpen:
        return None


def _try(df: pd.DataFrame, loan: Loan, capital: float) -> LoanResult | None:
    try:
        return simulate(df, loan, capital)
    except CannotOpen:
        return None


# --- línea de órdenes ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.aave_backtest", description=__doc__.split("\n\n")[0])
    ap.add_argument("tokens", nargs="+", metavar="GARANTIA DEUDA | cubierta POOL",
                    help="préstamo: token de garantía y token prestado (WETH USDC); liquidez cubierta: "
                         "«cubierta» y el pool (dirección o parte de la etiqueta, p. ej. «WETH/USDC 0.05%%»)")
    ap.add_argument("--red", help="ethereum | base | arbitrum (obligatoria en el préstamo)")
    ap.add_argument("--mercado", help="en Ethereum: ethereum (principal, por defecto) | lido | etherfi")
    ap.add_argument("--desde"), ap.add_argument("--hasta")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--hf", default=None, help="factor de salud al abrir, lista (por defecto 1.8; en la cubierta 2)")
    ap.add_argument("--gestion", default="nunca", help="nunca | defender, lista: nunca,defender")
    ap.add_argument("--defensa", type=float, default=1.3, help="HF que dispara la defensa")
    ap.add_argument("--ancho", default="10", help="cubierta: semiancho del rango en %%, lista: 5,10,20")
    ap.add_argument("--reajuste", default="nunca", help="cubierta: nunca | fuera, lista")
    ap.add_argument("--espera", type=int, default=0, help="cubierta: horas fuera de rango antes de reajustar")
    ap.add_argument("--gas", type=float, default=None, help="dólares por transacción (por defecto: 5 en Ethereum, 0,05 en L2)")
    ap.add_argument("--ventana", type=int, help="días de cada prueba: abrir en muchas fechas")
    ap.add_argument("--paso", type=int, default=7, help="días entre fechas de entrada (con --ventana)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.tokens[0].lower() == "cubierta":
        if len(a.tokens) != 2:
            ap.error("uso: cubierta POOL")
        return main_covered(a, a.tokens[1])
    if len(a.tokens) != 2 or not a.red:
        ap.error("préstamo: indica GARANTIA DEUDA y --red")
    a.garantia, a.deuda = a.tokens
    a.hf = a.hf or "1.8"
    try:
        info, df = load_pair(a.garantia, a.deuda, a.red, a.mercado, a.desde, a.hasta)
    except BacktestError as e:
        print(e, file=sys.stderr)
        return 2
    gas = a.gas if a.gas is not None else (5.0 if a.red == "ethereum" else 0.05)
    loans = [Loan(float(h), g, a.defensa, gas) for h, g in itertools.product(a.hf.split(","), a.gestion.split(","))]
    head = (f"Préstamo con garantía · {info['collateral']} de garantía, {info['debt']} prestado · Aave V3 "
            f"{info['market']} ({info['chain']}) · LTV {df['ltv'].iloc[-1]:.0%}, umbral {df['lt'].iloc[-1]:.0%}, "
            f"bonus {df['bonus'].iloc[-1]:.1%} (hoy)")
    try:
        if a.ventana:
            rows = []
            for loan in loans:
                rs = windows(df, lambda part, loan=loan: _try(part, loan, a.capital), a.ventana, a.paso)
                if rs and (row := summarize(rs, loan)):
                    rows.append(row)
            if a.json:
                print(json.dumps({**info, "ventana_dias": a.ventana, "resultados": rows}, default=str, indent=1))
                return 0
            print(f"{head}\nVentanas de {a.ventana} días cada {a.paso} días · {df['hour'].iloc[0]:%Y-%m-%d} → "
                  f"{df['hour'].iloc[-1]:%Y-%m-%d} · capital {a.capital:,.0f} $\n")
            print(f"{'estrategia':<24}{'n':>4}{'liquid.':>9}{'HF mín. med.':>13}{'HF mín. peor':>13}"
                  f"{'coste APR':>10}{'neto APR':>9}{'vs mantener':>12}{'peor 10%':>10}{'defensas':>9}{'aportado máx.':>14}")
            for r in rows:
                print(f"{r['strategy']:<24}{r['ventanas']:>4}{r['liquidadas']:>4} ({r['pct_liquidadas']:>3.0%})"
                      f"{r['hf_min_mediana']:>13.2f}{r['hf_min_peor']:>13.2f}{r['coste_apr_mediana']:>10.2%}"
                      f"{r['coste_neto_apr_mediana']:>9.2%}{_pct(r['vs_mantener_mediana']):>12}{_pct(r['vs_mantener_p10']):>10}"
                      f"{r['defensas_media']:>9.1f}{r['aportado_max']:>14,.0f}")
            if rows:
                print(f"\nHF inicial que habría evitado la liquidación sin tocar nada: en todas las ventanas "
                      f"{rows[0]['hf_necesario_max']:.2f}; en el 90 % de ellas {rows[0]['hf_necesario_p90']:.2f}.")
            blocked = {r["strategy"]: r["sin_abrir"] for r in rows if r["sin_abrir"]}
            if blocked:
                which = (f"{next(iter(blocked.values()))} ventanas" if len(set(blocked.values())) == 1
                         and len(blocked) == len(rows) else ", ".join(f"{k}: {v}" for k, v in blocked.items()))
                print(f"Sin abrir ({which}): Aave no dejaba abrir el préstamo en esa fecha "
                      "(LTV 0, reserva congelada o HF por debajo del LTV).")
            print("Coste APR = intereses pagados; neto = pagado − cobrado por tener lo prestado depositado en Aave. "
                  "vs mantener = resultado frente a solo depositar la garantía, sobre el capital.")
            return 0
        res = [simulate(df, loan, a.capital) for loan in loans]
    except BacktestError as e:
        print(e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({**info, "resultados": [asdict(r) for r in res]}, default=str, indent=1))
        return 0
    r0 = res[0]
    print(f"{head}\n{r0.start} → {r0.end} ({r0.days} días) · capital {a.capital:,.0f} $ · gas {gas} $/tx\n")
    print(f"{'estrategia':<24}{'deuda $':>9}{'HF mín.':>8}{'liquid.':>8}{'pérdida liq.':>13}{'intereses':>10}"
          f"{'coste APR':>10}{'neto APR':>9}{'final $':>10}{'mantener $':>11}{'vs mantener':>12}{'aportado':>9}")
    for r in res:
        print(f"{r.strategy:<24}{r.debt_usd:>9,.0f}{r.hf_min:>8.2f}{r.liquidations:>8}{r.liquidation_loss_usd:>13,.0f}"
              f"{r.interest_usd:>10,.0f}{r.borrow_apr:>10.2%}{r.net_cost_apr:>9.2%}{r.final_usd:>10,.0f}"
              f"{r.hold_usd:>11,.0f}{_pct(r.vs_hold_pct):>12}{r.added_usd:>9,.0f}")
    print(f"\nHF inicial que habría evitado la liquidación en este periodo sin tocar nada: {r0.hf_needed:.2f}.")
    return 0


def main_covered(a: argparse.Namespace, key: str) -> int:
    try:
        pool, df = load_covered(key, a.red, a.mercado, a.desde, a.hasta)
    except BacktestError as e:
        print(e, file=sys.stderr)
        return 2
    gas = a.gas if a.gas is not None else (5.0 if pool["chain"] == "ethereum" else 0.05)
    grid = [Covered(float(w) / 100, float(h), g, a.defensa, r, a.espera, gas)
            for w, r, h, g in itertools.product(a.ancho.split(","), a.reajuste.split(","),
                                                (a.hf or "2").split(","), a.gestion.split(","))]
    head = (f"Liquidez cubierta · {pool['label']} · {pool['chain']} {pool['version']} · {pool['pool_id']}\n"
            f"{pool['collateral']} en Aave V3 {pool['market']} como garantía, {pool['debt']} prestado al pool · "
            f"umbral {df['lt'].iloc[-1]:.0%}, bonus {df['bonus'].iloc[-1]:.1%} (hoy)")
    if pool.get("aviso") and not a.json:
        head += f"\nAviso: {pool['aviso']}"
    try:
        if a.ventana:
            rows = []
            for cv in grid:
                rs = windows(df, lambda part, cv=cv: _try_covered(pool, part, cv, a.capital), a.ventana, a.paso)
                if rs and (row := summarize_covered(rs, cv)):
                    rows.append(row)
            if a.json:
                print(json.dumps({"pool": pool, "ventana_dias": a.ventana, "resultados": rows}, default=str, indent=1))
                return 0
            print(f"{head}\nVentanas de {a.ventana} días cada {a.paso} días · {df['hour'].iloc[0]:%Y-%m-%d} → "
                  f"{df['hour'].iloc[-1]:%Y-%m-%d} · capital {a.capital:,.0f} $ · gas {gas} $/tx\n")
            print(f"{'estrategia':<30}{'n':>4}{'mediana':>9}{'peor 10%':>10}{'>0':>5}{'vs ' + pool['collateral']:>10}"
                  f"{'peor 10%':>10}{'vs sin cubrir':>14}{'APR com.':>9}{'Aave neto':>10}{'liquid.':>8}{'HF mín.':>8}{'en pool':>8}")
            for r in rows:
                print(f"{r['strategy']:<30}{r['ventanas']:>4}{_pct(r['pnl_mediana']):>9}{_pct(r['pnl_p10']):>10}"
                      f"{r['pct_positivas']:>5.0%}{_pct(r['vs_estable_mediana']):>10}{_pct(r['vs_estable_p10']):>10}"
                      f"{_pct(r['vs_sin_cubrir_mediana']):>14}{r['fees_apr_mediana']:>9.1%}{r['aave_neto_apr_mediana']:>+10.1%}"
                      f"{r['liquidadas']:>8}{r['hf_min_peor']:>8.2f}{r['pool_pct_capital']:>8.0%}")
            if rows:
                print(f"\nHF inicial que habría evitado la liquidación (subida de {pool['debt']}) en todas las ventanas: "
                      f"{rows[0]['hf_necesario_max']:.2f}.")
            print(f"vs {pool['collateral']} = frente a dejar todo el capital depositado en Aave; vs sin cubrir = frente al "
                  "mismo pool con todo el capital y sin préstamo. Aave neto = lo que cobra la garantía − intereses del "
                  "préstamo, anual sobre el capital (+ = ingreso). En pool = tamaño de la posición sobre el capital.")
            return 0
        res = [simulate_covered(pool, df, cv, a.capital) for cv in grid]
    except BacktestError as e:
        print(e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"pool": pool, "resultados": [asdict(r) for r in res]}, default=str, indent=1))
        return 0
    r0 = res[0]
    print(f"{head}\n{r0.start} → {r0.end} ({r0.days} días) · capital {a.capital:,.0f} $ · gas {gas} $/tx\n")
    print(f"{'estrategia':<30}{'en pool $':>10}{'garantía $':>11}{'deuda $':>9}{'final $':>9}{'resultado':>10}"
          f"{'comisiones':>11}{'intereses':>10}{'garantía +':>11}{'HF mín.':>8}{'liquid.':>8}{'vs ' + pool['collateral']:>10}"
          f"{'vs sin cubrir':>14}")
    for r in res:
        print(f"{r.strategy:<30}{r.lp_value:>10,.0f}{r.deposit:>11,.0f}{r.debt_usd:>9,.0f}{r.final_usd:>9,.0f}"
              f"{_pct(r.pnl_pct):>10}{r.fees_usd:>11,.0f}{r.interest_usd:>10,.0f}{r.collateral_yield_usd:>11,.0f}"
              f"{r.hf_min:>8.2f}{r.liquidations:>8}{_pct(r.vs_stable_pct):>10}{_pct(r.vs_unhedged_pct):>14}")
    print(f"\nHF inicial que habría evitado la liquidación en este periodo: {r0.hf_needed:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
