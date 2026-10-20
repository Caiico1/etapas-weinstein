"""Backtest de estrategias de Aave V3 contra el histórico horario de MetaProDB (tablas aave_*,
leídas de la blockchain): por ahora, el préstamo con garantía.

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

Medida clave: sin liquidación, HF(t) = HF inicial × r(t), con r(t) independiente del HF elegido.
Así que el HF inicial que habría aguantado una ventana es 1 / mín r(t) («HF necesario»).

    python -m etapas.aave_backtest WETH USDC --red base --hf 1.5,1.8,2.2 --ventana 60 --paso 7
    python -m etapas.aave_backtest cbBTC USDC --red base --hf 1.8 --gestion nunca,defender --desde 2025-01-01

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


def simulate(df: pd.DataFrame, loan: Loan, capital: float = 10_000.0) -> LoanResult:
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
    sp = sd * d_idx[0] / d_sidx[0]                          # lo prestado, depositado en Aave (escalado)
    gas = 4 * loan.gas_usd
    hf_min, below, liqs, liq_usd, liq_loss, defs, added = float("inf"), 0, 0, 0.0, 0.0, 0, 0.0
    interest = carry = debt_hours = 0.0

    for i in range(len(df)):
        if i:
            interest += sd * (d_idx[i] - d_idx[i - 1]) * d_px[i]
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
                      defs, added, interest, apr(interest), carry, apr(interest - carry), final, hold,
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
    ap.add_argument("garantia", help="token de garantía (WETH, cbBTC, wstETH...)")
    ap.add_argument("deuda", help="token que se pide prestado (USDC, USDT, GHO...)")
    ap.add_argument("--red", required=True, help="ethereum | base | arbitrum")
    ap.add_argument("--mercado", help="en Ethereum: ethereum (principal, por defecto) | lido | etherfi")
    ap.add_argument("--desde"), ap.add_argument("--hasta")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--hf", default="1.8", help="factor de salud al abrir, lista: 1.5,1.8,2.2")
    ap.add_argument("--gestion", default="nunca", help="nunca | defender, lista: nunca,defender")
    ap.add_argument("--defensa", type=float, default=1.3, help="HF que dispara la defensa")
    ap.add_argument("--gas", type=float, default=None, help="dólares por transacción (por defecto: 5 en Ethereum, 0,05 en L2)")
    ap.add_argument("--ventana", type=int, help="días de cada prueba: abrir en muchas fechas")
    ap.add_argument("--paso", type=int, default=7, help="días entre fechas de entrada (con --ventana)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
