"""Mejores oportunidades de liquidez concentrada para un par (ETH/USDC, ETH/BTC...).

    python -m etapas.oportunidad ETH/USDC --capital 5000
    python -m etapas.oportunidad ETH/BTC --capital 5000 --json

Para cada ciclo (diario, semanal, mensual) y cada pool verificado del par:
  1. Idoneidad según la etapa de Weinstein del par en ese marco (liquidez.verdict).
  2. Rango con el método validado del proyecto (liquidez.lp_range), ajustado a los ticks del pool.
  3. Comportamiento histórico del mismo método en este par, fuera de muestra: fracción de ciclos en
     que el precio no salió del rango y resultado medio frente a mantener los tokens.
  4. Comisiones para el capital: lo que cobró de verdad cada unidad de liquidez del pool en los
     últimos 7 y 30 días (el menor), leído de la blockchain (pools.realized_fees). Si la red no tiene nodo con
     histórico (Solana) o falla, estimación con la mediana del volumen diario de 30 días y la
     liquidez activa actual, ya descontada la comisión del protocolo.
  5. Gas de un reajuste por ciclo y cambio de tokens esperado al reajustar (cuando el precio sale
     del rango hay que cambiar ~la mitad de la posición).
  6. Stop-loss: precio que el par no alcanzó en el STOP_QUANTILE de los ciclos pasados, con lo que
     se perdería frente a USDC al tocarlo y su comportamiento histórico (cuántas veces saltó).
Resultado neto por ciclo = comisiones × fracción de ciclos dentro + resultado medio frente a
mantener × capital − gas − cambio de tokens. Se proponen las dos mejores combinaciones (de ciclos
distintos si es posible) entre las que no tienen etapa desfavorable y dan un resultado neto positivo.
"""
import argparse
import json
import math
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import aave as A
from . import pools as P
from .analysis import AssetResult, TimeframeResult, analyze_symbol
from .config import DISCLAIMER, LP_QUANTILE, TIMEFRAMES
from .data import STABLES, DataError, canonical_pair, fetch_candles, fetch_pair_candles
from .descubrir import discover, supported_symbols
from .indicators import expected_range
from .liquidez import (PoolQuote, amounts_per_liquidity, composition, divergence, lp_range, position_value,
                       quote_pool, verdict)
from .pools import Pool

CYCLES = [("1d", "Diario", 1.0), ("1w", "Semanal", 7.0), ("1M", "Mensual", 30.44)]
HIGHER = {"1d": "1w", "1w": "1M", "1M": None}
BACKTEST = {"1d": 365, "1w": 104, "1M": 48}          # ciclos pasados que se comprueban
MIN_BACKTEST = 24
MIN_TVL = 1_000_000                                   # con menos, 5.000 $ pesan demasiado en el pool
MIN_VOLUME = 50_000                                   # volumen diario mínimo (mediana 30 días)
REBALANCE_GAS = 900_000                               # retirar + cobrar + cambiar + abrir (Uniswap v3)
L2_DATA_FEE_USD = 0.05                                # margen por la tarifa de datos de L1 en Base/Arbitrum
SOLANA_REBALANCE_SOL = 0.001                          # comisiones de transacción y prioridad en Solana
NO_DAILY_ON = {"ethereum"}                            # el ciclo diario no se propone en Ethereum (gas)
# Margen de seguridad: la pérdida frente a mantener, medida por anchura del rango, varía hasta un
# 20-30 % entre mitades del histórico (BTC, SOL). Solo se propone lo que sigue siendo rentable con
# una pérdida un 25 % peor que la estimada.
SAFETY_LOSS = 1.25
ALIASES = {"WETH": "ETH", "WBTC": "BTC", "CBBTC": "BTC", "WSOL": "SOL", "USDT0": "USDT"}
# Activos sin histórico suficiente que siguen el mismo precio que otro: XAUt y PAXG son una onza
# de oro cada uno (XAUT cotiza en Binance solo desde marzo de 2026).
PRICE_PROXY = {"XAUT": "PAXG"}
# Stop-loss: precio que el par no alcanzó en el 99 % de los ciclos pasados (el rango usa el 90 %).
# Así salta solo ante caídas fuera de lo normal y no en cada salida del rango.
STOP_QUANTILE = 0.99
# ... pero nunca más de un 10 % más allá del borde del rango: en el ciclo mensual el cuantil 99 % queda
# a veces a −50 % del precio, demasiado lejos para cortar pérdidas.
STOP_MAX_GAP = 0.10
SWAP_SLIPPAGE = 0.0005                                # deslizamiento supuesto al cambiar tokens
# Permanencia en el rango y estrategia pasiva: se abre el rango del ciclo y se deja sin tocar durante
# HORIZON_DAYS días, recorriendo las velas diarias que siguieron a cada ciclo pasado.
HORIZON_DAYS = 30
MIN_PERMANENCE = 12                                   # rangos pasados mínimos para dar el dato
INFLATED_24H = 2.0                                    # aviso si las comisiones de 24 h doblan las de 30 días
# Criterios de eficiencia del pool (avisos, no vetos: las comisiones reales ya los miden mejor)
VOL_TVL_REF = 1.0                                     # volumen diario mayor que el TVL
FEES_TVL_REF = 0.001                                  # comisiones del pool ≥ 0,1 % del TVL al día
# Estables sin respaldo 1:1 en efectivo y deuda pública a corto plazo
STABLE_BACKING = {
    "USDT": "Tether: parte de las reservas en BTC, oro y préstamos, no solo efectivo y letras del Tesoro",
    "DAI": "respaldada por préstamos con colateral cripto, no por efectivo",
}
STABLE_TOKENS = {"USDC", "USDT", "DAI"}
# Tokens que dependen de un custodio o de un puente (riesgo distinto del de precio)
TOKEN_CUSTODY = {"WBTC": "custodiado por BitGo", "cbBTC": "custodiado por Coinbase",
                 "WETH (Wormhole)": "puenteado con Wormhole", "PAXG": "oro custodiado por Paxos",
                 "XAUt": "oro custodiado por Tether"}
# Protocolos admitidos: más de 1.000 M$ de TVL y años en producción, incluidos mercados bajistas
CONSOLIDATED_DEX = {"Uniswap v3": "Uniswap v3, desde 2021", "Uniswap v4": "Uniswap desde 2018, v4 desde 2025",
                    "Orca": "Orca desde 2021, Whirlpools desde 2022"}


@dataclass
class Backtest:
    inside: float | None          # fracción de ciclos en que el precio no salió del rango
    mean_result: float | None     # resultado medio frente a mantener (negativo = pérdida)
    samples: int
    per_width: float | None = None   # resultado medio por unidad de semianchura del rango (log)
    stop_hit: float | None = None    # fracción de ciclos en que se habría tocado el stop-loss
    mean_with_stop: float | None = None   # resultado medio frente a mantener saliendo en el stop

    def expected(self, low: float, high: float) -> float | None:
        """Resultado esperado frente a mantener para un rango [low, high]. El método ajusta la
        anchura del rango a la volatilidad de cada momento, así que el resultado es proporcional a
        la anchura: se reescala la media histórica a la anchura actual, en lugar de mezclar la
        volatilidad de todo el histórico con las comisiones de hoy."""
        return None if self.per_width is None else self.per_width * math.log(high / low) / 2


@dataclass
class Permanence:
    """Qué pasó en los HORIZON_DAYS días siguientes a abrir el rango del método, sin tocarlo."""
    samples: int
    days_to_exit: float | None = None   # mediana de días con el precio dentro antes de la primera salida
    inside: float | None = None         # fracción de los días con el cierre dentro del rango
    exits: float | None = None          # fracción de rangos de los que el precio salió en algún momento
    up: float | None = None             # de esas salidas, fracción por arriba
    per_width: float | None = None      # resultado frente a mantener al final, por unidad de semianchura

    def expected(self, low: float, high: float) -> float | None:
        """Resultado frente a mantener sin tocar el rango, reescalado a su anchura (como Backtest)."""
        return None if self.per_width is None else self.per_width * math.log(high / low) / 2


@dataclass
class Opportunity:
    cycle: str
    name: str
    days: float
    level: str
    reasons: list[str]
    pool: Pool
    quote: PoolQuote
    backtest: Backtest
    gas_usd: float | None = None
    expected_result: float | None = None   # resultado esperado frente a mantener, con el rango actual
    fees_cycle: float | None = None
    net_cycle: float | None = None
    net_safe: float | None = None          # neto con la pérdida un SAFETY_LOSS peor
    net_apr: float | None = None
    base_amount: float | None = None
    quote_amount: float | None = None
    swap_cycle: float | None = None        # cambio de tokens esperado por ciclo al reajustar ($)
    entry_swapped: float | None = None     # dólares que hay que cambiar desde USDC para abrir
    entry_cost: float | None = None        # coste de ese cambio ($)
    stop_low: float | None = None          # stop-loss por abajo (precio del par)
    stop_high: float | None = None         # ... y por arriba, solo entre dos volátiles
    scenarios: dict = field(default_factory=dict)   # resultado frente a USDC (fracción) en cada precio
    vol_tvl: float | None = None           # volumen diario (mediana 30 días) / TVL
    fees_tvl: float | None = None          # comisiones diarias de todo el pool / TVL
    # APR de comisiones sobre el capital con el precio dentro del rango, por ventana de datos
    apr_windows: dict = field(default_factory=dict)
    fee_apr_cycle: float | None = None     # APR de comisiones reajustando cada ciclo (sin IL)
    passive_fees: float | None = None      # sin tocar HORIZON_DAYS días: comisiones ($)
    passive_il: float | None = None        # ... resultado frente a mantener ($)
    passive_net: float | None = None       # ... neto, con el gas de abrir
    passive_fee_apr: float | None = None
    passive_apr: float | None = None
    net_safe_apr: float | None = None      # neto con margen de seguridad, anualizado
    # Alternativa en Aave V3: depositar los mismos tokens que pide la posición (en su proporción)
    aave_yields: dict = field(default_factory=dict)    # {token: APY de depósito}
    aave_apy: float | None = None
    excluded: str = ""


@dataclass
class Report:
    base: str
    quote: str
    capital: float
    price: float | None
    quote_usd: float | None
    asset: AssetResult | None
    stages: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)
    backtests: dict = field(default_factory=dict)
    stops: dict = field(default_factory=dict)
    permanence: dict = field(default_factory=dict)
    opportunities: list[Opportunity] = field(default_factory=list)
    proposals: list[Opportunity] = field(default_factory=list)
    illiquid: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    hedge_ltv: float = 0.35                # LTV de la liquidez cubierta (estrategia.HEDGE_LTV)
    hedges: list = field(default_factory=list)   # estrategia.Hedge: estable en Aave + activo prestado


# ---------------------------------------------------------------- par


def parse_pair(text: str) -> tuple[str, str, list[str]]:
    """'eth/usdc' → ('ETH', 'USDC'). Admite '/', '-' y espacios, y nombres de tokens envueltos
    (WETH, WBTC, cbBTC). Con la estable delante (USDC/ETH) se da la vuelta: el precio se expresa
    siempre en la moneda estable."""
    parts = [x for x in text.upper().replace("-", "/").replace(" ", "/").split("/") if x]
    if len(parts) != 2:
        raise ValueError(f"'{text}' no es un par: escribe dos símbolos, por ejemplo ETH/USDC, o una "
                         f"temática: oro, bitcoin, ether")
    base, quote = (ALIASES.get(x, x) for x in parts)
    notes = []
    if base in STABLES and quote not in STABLES:
        base, quote = quote, base
        notes.append(f"Par expresado como {base}/{quote} (precio en la moneda estable)")
    if base == quote or (base in STABLES and quote in STABLES):
        raise ValueError("Pares entre dos estables o de un token consigo mismo no se analizan: las "
                         "etapas de Weinstein no tienen sentido en ellos")
    unknown = [x for x in (base, quote) if x not in supported_symbols()]
    if unknown:
        raise ValueError(f"Sin tokens verificados para {', '.join(unknown)}. Disponibles: "
                         f"{', '.join(supported_symbols())} (se añaden en etapas/descubrir.py)")
    return base, quote, notes


# ---------------------------------------------------------------- piezas


def stop_levels(lo: float, hi: float, s_lo: float, s_hi: float, cross: bool) -> tuple[float, float | None]:
    """Stop-loss por abajo (y por arriba entre dos volátiles), nunca dentro del rango ni más de
    STOP_MAX_GAP más allá de su borde. Con una estable no hay stop por arriba: al salir por ahí la
    posición ya queda en la estable."""
    low = max(min(s_lo, lo), lo * (1 - STOP_MAX_GAP))
    high = min(max(s_hi, hi), hi * (1 + STOP_MAX_GAP)) if cross else None
    return low, high


def backtest(r: TimeframeResult, samples: int, cross: bool = False) -> Backtest:
    """Aplica el método del rango a cada uno de los últimos `samples` ciclos con los datos que había
    al empezar cada uno (fuera de muestra) y mide qué pasó en ese ciclo. También mide el stop-loss:
    cuántas veces se habría tocado y el resultado frente a mantener saliendo en él (la posición se
    cierra al precio del stop y se queda así hasta el cierre del ciclo)."""
    if r.status != "ok" or r.candles is None or r.history is None:
        return Backtest(None, None, 0)
    cfg = TIMEFRAMES[r.timeframe]
    df, atr = r.candles, r.history["atr"]
    inside, results, scaled, hits, stopped = [], [], [], [], []
    for t in range(max(0, len(df) - 1 - samples), len(df) - 1):
        lo, hi = expected_range(df.iloc[:t + 1], atr.iloc[:t + 1], LP_QUANTILE, cfg.range_lookback)
        if pd.isna(lo) or pd.isna(hi) or lo <= 0:
            continue
        s_lo, s_hi = expected_range(df.iloc[:t + 1], atr.iloc[:t + 1], STOP_QUANTILE, cfg.range_lookback)
        c, nxt = df["close"].iloc[t], df.iloc[t + 1]
        inside.append(nxt["low"] >= lo and nxt["high"] <= hi)
        results.append(divergence(c, nxt["close"], lo, hi))
        scaled.append(results[-1] / (math.log(hi / lo) / 2))
        stop_lo, stop_hi = stop_levels(lo, hi, s_lo, s_hi, cross)
        hit = (stop_lo if stop_lo > 0 and nxt["low"] <= stop_lo else
               stop_hi if stop_hi is not None and nxt["high"] >= stop_hi else None)
        hits.append(hit is not None)
        if hit is None:
            stopped.append(results[-1])
        else:                       # sale al precio del stop y se compara con mantener al cierre
            x0, y0 = amounts_per_liquidity(c, lo, hi)
            stopped.append(position_value(hit, lo, hi) / (x0 * nxt["close"] + y0) - 1)
    if len(inside) < MIN_BACKTEST:
        return Backtest(None, None, len(inside))
    return Backtest(float(np.mean(inside)), float(np.mean(results)), len(inside), float(np.mean(scaled)),
                    float(np.mean(hits)), float(np.mean(stopped)))


def permanence(r: TimeframeResult, daily: pd.DataFrame | None, samples: int,
               horizon: int = HORIZON_DAYS) -> Permanence:
    """Abre el rango del método al cierre de cada uno de los últimos `samples` ciclos (con los datos
    de entonces) y recorre las `horizon` velas diarias siguientes sin tocarlo: cuántos días aguanta
    el precio dentro, qué parte del tiempo cierra dentro, por qué lado sale y el resultado frente a
    mantener al final."""
    if r.status != "ok" or r.candles is None or r.history is None or daily is None or daily.empty:
        return Permanence(0)
    cfg = TIMEFRAMES[r.timeframe]
    df, atr = r.candles, r.history["atr"]
    days, inside, exited, up, scaled = [], [], [], [], []
    for t in range(max(0, len(df) - 1 - samples), len(df) - 1):
        lo, hi = expected_range(df.iloc[:t + 1], atr.iloc[:t + 1], LP_QUANTILE, cfg.range_lookback)
        if pd.isna(lo) or pd.isna(hi) or lo <= 0:
            continue
        start = df.index[t + 1]
        path = daily.loc[daily.index >= start].iloc[:horizon]
        # El tramo diario tiene que empezar al abrirse el ciclo (el histórico diario es más corto)
        if len(path) < horizon or path.index[0] - start > pd.Timedelta(days=1):
            continue
        c = df["close"].iloc[t]
        out = ((path["low"] < lo) | (path["high"] > hi)).to_numpy()
        first = int(np.argmax(out)) if out.any() else horizon
        days.append(first)
        exited.append(bool(out.any()))
        if out.any():
            row = path.iloc[first]
            hit_hi, hit_lo = row["high"] > hi, row["low"] < lo
            up.append(bool(hit_hi and (not hit_lo or row["close"] > c)))
        inside.append(float(((path["close"] >= lo) & (path["close"] <= hi)).mean()))
        scaled.append(divergence(c, path["close"].iloc[-1], lo, hi) / (math.log(hi / lo) / 2))
    if len(days) < MIN_PERMANENCE:
        return Permanence(len(days))
    return Permanence(len(days), float(np.median(days)), float(np.mean(inside)), float(np.mean(exited)),
                      float(np.mean(up)) if up else None, float(np.mean(scaled)))


def stop_range(r: TimeframeResult, center: float) -> tuple[float, float] | None:
    """Precios que el par no alcanzó, por cada lado, en el STOP_QUANTILE de los ciclos pasados."""
    if r.status != "ok" or r.candles is None or r.history is None:
        return None
    lo, hi = expected_range(r.candles, r.history["atr"], STOP_QUANTILE, TIMEFRAMES[r.timeframe].range_lookback,
                            center=center)
    return None if pd.isna(lo) or pd.isna(hi) else (float(lo), float(hi))


def gas_costs(chains: set[str]) -> tuple[dict[str, float], list[str]]:
    """Coste en dólares de un reajuste en cada red, con el precio del gas actual."""
    costs, warnings = {}, []
    prices = {}
    for sym in ("ETH", "SOL"):
        try:
            c = fetch_candles(sym, TIMEFRAMES["1d"])
            prices[sym] = float(c.current["close"] if c.current is not None else c.df["close"].iloc[-1])
        except DataError:
            pass
    for chain in chains:
        try:
            if chain == "solana":
                costs[chain] = SOLANA_REBALANCE_SOL * prices["SOL"]
            else:
                gwei = int(P._rpc(chain, "eth_gasPrice", []), 16) / 1e9
                costs[chain] = REBALANCE_GAS * gwei * 1e-9 * prices["ETH"]
                if chain != "ethereum":
                    costs[chain] += L2_DATA_FEE_USD
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append(f"Sin precio del gas en {chain}: no se descuenta")
    return costs, warnings


def _quote_usd(quote: str) -> float:
    if quote in STABLES:
        return 1.0
    c = fetch_candles(quote, TIMEFRAMES["1d"])
    return float(c.current["close"] if c.current is not None else c.df["close"].iloc[-1])


# ---------------------------------------------------------------- análisis completo


def analyze(pair: str, capital: float = 5000.0, chains=None, hedge_ltv: float | None = None) -> Report:
    """Analiza un par. `chains` limita los pools a esas redes (por ejemplo, ["base"]). `hedge_ltv`:
    LTV de la liquidez cubierta (por defecto, estrategia.HEDGE_LTV)."""
    from . import estrategia as E                  # estrategia importa este módulo
    rep = _analyze(pair, capital, chains)
    rep.hedge_ltv = E.HEDGE_LTV if hedge_ltv is None else hedge_ltv
    rep.hedges = E.evaluate(rep, rep.hedge_ltv)
    return rep


def _analyze(pair: str, capital: float, chains) -> Report:
    try:
        base, quote, notes = parse_pair(pair)
    except ValueError as e:
        return Report(pair, "", capital, None, None, None, error=str(e))
    cb, cq = canonical_pair(base, quote)
    if (cb, cq) != (base, quote):
        notes.append(f"Se analiza como {cb}/{cq}, la orientación en que cotiza el par: es el mismo pool y "
                     f"el mismo rango. En la propuesta tienes también los precios en {base}/{quote}.")
        base, quote = cb, cq
    rep = Report(base, quote, capital, None, None, None, warnings=list(notes))
    series = PRICE_PROXY.get(base, base)
    if series != base:
        rep.warnings.append(f"Etapas y rangos de {base} calculados con el histórico de {series}, que sigue "
                            f"el mismo precio y tiene más años de datos")
    asset = analyze_symbol(base, fetch=lambda t, cfg: fetch_pair_candles(series, quote, cfg))
    if asset.error or not asset.last_price:
        rep.error = f"No hay histórico de precios de {base}/{quote}: {asset.error}"
        return rep
    rep.asset, rep.price = asset, asset.last_price
    try:
        rep.quote_usd = _quote_usd(quote)
    except DataError as e:
        rep.error = f"No hay precio en dólares de {quote}: {e}"
        return rep

    daily = asset.timeframes["1d"].candles if asset.timeframes["1d"].status == "ok" else None
    for key, name, _ in CYCLES:
        r = asset.timeframes[key]
        up = asset.timeframes.get(HIGHER[key]) if HIGHER[key] else None
        rep.stages[key] = verdict(r, up, base, quote)
        rep.ranges[key] = lp_range(r, rep.price)
        rep.backtests[key] = backtest(r, BACKTEST[key], cross=quote not in STABLES)
        rep.stops[key] = stop_range(r, rep.price)
        rep.permanence[key] = permanence(r, daily, BACKTEST[key])

    found, warn = discover(base, quote)
    rep.warnings += warn
    if chains:
        found = [p for p in found if p.chain in chains]
        rep.warnings.append("Solo se evalúan pools en: " + ", ".join(c.capitalize() for c in chains))
    if not found:
        rep.warnings.append(f"No hay pools de Uniswap v3, v4 ni de Orca para {base}/{quote} con los tokens verificados")
        return rep
    states = P.fetch_states(found)
    liquid = []
    for pool in found:
        st = states.get(pool.id)
        if st is None or st.price is None:
            rep.illiquid.append(f"{pool.label}: sin datos ({st.error if st else 'sin estado'})")
            continue
        if st.tvl is None or st.tvl < MIN_TVL:
            rep.illiquid.append(f"{pool.label}: TVL {'desconocido' if st.tvl is None else f'{st.tvl / 1e3:,.0f} k$'}")
            continue
        base_usd = rep.price * rep.quote_usd
        usd0, usd1 = (base_usd, rep.quote_usd) if pool.base_is_token0 else (rep.quote_usd, base_usd)
        # Prudencia: el menor entre lo cobrado a 7 y a 30 días (una semana buena no se extrapola)
        rates = P.fee_rates(pool, usd0, usd1)
        real = rates.prudent if rates else None
        vol = P.median_volume(pool)
        if (vol is None or vol < MIN_VOLUME) and real and st.liquidity:
            # El volumen de GeckoTerminal a veces no cuadra con lo que el pool cobró de verdad: manda la
            # blockchain (volumen que explica esas comisiones con la liquidez activa de hoy)
            implied = real * st.liquidity / (pool.fee * (1 - st.protocol_cut))
            if implied >= MIN_VOLUME:
                rep.warnings.append(f"{pool.label}: el volumen de GeckoTerminal "
                                    f"({'desconocido' if vol is None else f'{vol / 1e3:,.0f} k$/día'}) no cuadra con "
                                    f"las comisiones cobradas on-chain; se usa el que las explica "
                                    f"({implied / 1e3:,.0f} k$/día)")
                vol = implied
        if vol is None or vol < MIN_VOLUME:
            rep.illiquid.append(f"{pool.label}: volumen diario {'desconocido' if vol is None else f'{vol / 1e3:,.0f} k$'}")
            continue
        liquid.append((pool, st, vol, real, rates))
    unknown = sum(1 for p in found if (st := states.get(p.id)) and st.price is not None and st.tvl is None)
    if unknown:
        rep.warnings.append(f"GeckoTerminal no devolvió el TVL de {unknown} pools: el análisis puede estar "
                            f"incompleto; vuelve a ejecutarlo en unos minutos")
    gas, warn = gas_costs({x[0].chain for x in liquid})
    rep.warnings += warn
    # Cambiar tokens: en el pool más barato de la red, más un deslizamiento prudente
    swap_rate = {}
    for pool, *_ in liquid:
        swap_rate[pool.chain] = min(swap_rate.get(pool.chain, 1.0), pool.fee + SWAP_SLIPPAGE)
    cross = quote not in STABLES

    for key, name, days in CYCLES:
        level, reasons = rep.stages[key]
        rng, bt, pm = rep.ranges[key], rep.backtests[key], rep.permanence[key]
        for pool, st, vol, real, rates in liquid:
            q = (quote_pool(pool, st, rng[0], rng[1], rep.price, capital, rep.quote_usd, vol, real)
                 if rng else PoolQuote(pool, error="sin rango: historial insuficiente"))
            o = Opportunity(key, name, days, level, reasons, pool, q, bt, gas.get(pool.chain))
            if st.tvl:
                # Comisiones de todo el pool al día: lo cobrado por unidad de liquidez × liquidez activa
                pool_fees = (real * st.liquidity if real is not None and st.liquidity
                             else vol * pool.fee * (1 - st.protocol_cut))
                o.vol_tvl, o.fees_tvl = vol / st.tvl, pool_fees / st.tvl
            if q.fee_day is not None and q.fee_source == "real" and rates and real:
                # Las comisiones para tu capital son proporcionales a lo cobrado por unidad de liquidez
                scale = q.fee_day / real * 365 / capital
                o.apr_windows = {k: v * scale for k, v in (("24 h", rates.day), ("7 d", rates.week),
                                                           ("30 d", rates.month),
                                                           ("semana floja", rates.weakest_week)) if v is not None}
            if rng is None:
                o.excluded = "sin rango: historial insuficiente"
            elif key == "1d" and pool.chain in NO_DAILY_ON:
                o.excluded = "el ciclo diario no se propone en Ethereum (coste del gas)"
            elif q.fee_day is None:
                o.excluded = q.error or "sin estimación de comisiones"
            elif bt.inside is None:
                o.excluded = f"historial insuficiente para comprobar el método ({bt.samples} ciclos)"
            elif o.gas_usd is None:
                o.excluded = "sin precio del gas: no se puede calcular el neto"
            else:
                share = composition(q.pool_price, q.low, q.high)
                rate = swap_rate[pool.chain]
                # Al salir del rango la posición queda en un solo token: reajustar cambia ~la mitad
                o.swap_cycle = (1 - bt.inside) * 0.5 * capital * rate
                # Desde USDC: se compra la parte del activo (entre dos volátiles, los dos tokens)
                o.entry_swapped = capital if cross else capital * share
                o.entry_cost = o.entry_swapped * rate
                o.fees_cycle = q.fee_day * days * bt.inside
                o.expected_result = bt.expected(q.low, q.high)
                o.net_cycle = o.fees_cycle + o.expected_result * capital - o.gas_usd - o.swap_cycle
                o.net_safe = o.fees_cycle + SAFETY_LOSS * o.expected_result * capital - o.gas_usd - o.swap_cycle
                o.net_apr = o.net_cycle / capital * 365 / days
                o.net_safe_apr = o.net_safe / capital * 365 / days
                o.fee_apr_cycle = o.fees_cycle / capital * 365 / days
                toks = pool.pair.split("/")
                base_tok, quote_tok = (toks[0], toks[1]) if pool.base_is_token0 else (toks[1], toks[0])
                ya, yq = A.supply_yield(pool.chain, base_tok), A.supply_yield(pool.chain, quote_tok)
                if ya is not None and yq is not None:
                    o.aave_yields = {base_tok: ya, quote_tok: yq}
                    o.aave_apy = share * ya + (1 - share) * yq
                passive = pm.expected(q.low, q.high)
                if pm.inside is not None and passive is not None:
                    o.passive_fees = q.fee_day * HORIZON_DAYS * pm.inside
                    o.passive_il = passive * capital
                    o.passive_net = o.passive_fees + o.passive_il - o.gas_usd
                    o.passive_fee_apr = o.passive_fees / capital * 365 / HORIZON_DAYS
                    o.passive_apr = o.passive_net / capital * 365 / HORIZON_DAYS
                o.base_amount = capital * share / (q.pool_price * rep.quote_usd)
                o.quote_amount = capital * (1 - share) / rep.quote_usd
                v0 = position_value(q.pool_price, q.low, q.high)
                if rep.stops.get(key):
                    o.stop_low, o.stop_high = stop_levels(q.low, q.high, *rep.stops[key], cross)
                for label, price in (("min", q.low), ("max", q.high), ("stop", o.stop_low),
                                     ("stop_alto", o.stop_high)):
                    if price:
                        o.scenarios[label] = (price, position_value(price, q.low, q.high) / v0 - 1)
                if level == "desfavorable":
                    o.excluded = "etapa desfavorable"
                elif level == "sin datos":
                    o.excluded = "sin etapa"
                elif o.net_cycle <= 0:
                    o.excluded = "resultado neto estimado negativo"
                elif o.net_safe <= 0:
                    o.excluded = "rentable solo por poco: negativo con el margen de seguridad"
                elif o.aave_apy is not None and o.net_safe_apr <= o.aave_apy:
                    o.excluded = "rinde menos que depositar los mismos tokens en Aave"
            rep.opportunities.append(o)
    rep.proposals = choose(rep.opportunities)
    return rep


def choose(opps: list[Opportunity]) -> list[Opportunity]:
    """La mejor combinación y la mejor de otro ciclo (si no la hay, la mejor en otro pool)."""
    ok = sorted((o for o in opps if not o.excluded), key=lambda o: o.net_apr, reverse=True)
    if not ok:
        return []
    first = ok[0]
    second = (next((o for o in ok[1:] if o.cycle != first.cycle), None)
              or next((o for o in ok[1:] if o.pool.id != first.pool.id), None))
    return [first] + ([second] if second else [])


# ---------------------------------------------------------------- salida


def _p(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) < 10 else f"{x:,.2f}"
    return f"{x:.6g}"


def _pct(x: float | None, sign: bool = True) -> str:
    return "—" if x is None else (f"{x:+.1%}" if sign else f"{x:.0%}")


def _usd(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f} $"


def _stage_line(rep: Report, key: str) -> str:
    r = rep.asset.timeframes[key]
    if r.status != "ok":
        return f"sin datos ({r.message})"
    trans = f" (en transición {r.transition})" if r.transition else ""
    return f"etapa {r.stage} · {r.stage_name}{trans}, confianza {r.confidence_label}"


def _scen_txt(rep: Report, o: Opportunity, key: str) -> str:
    """'−4,1 %, −205,00 $ frente a USDC' para un escenario de precio (sin comisiones)."""
    price, ret = o.scenarios[key]
    unit = "frente a USDC" if rep.quote in STABLES else f"medido en {rep.quote}"
    return f"{price / o.quote.pool_price - 1:+.1%} desde el precio; {ret:+.1%}, {ret * rep.capital:+,.2f} $ {unit}"


OK, WARN, FAIL = "✅", "⚠️", "❌"
GAS_SHARE_WARN = 0.10            # aviso (no veto) si el gas se lleva más del 10 % de las comisiones del ciclo


def checklist(rep: Report, o: Opportunity) -> tuple[list[tuple[str, str, str, str]], str]:
    """Condiciones del método para una combinación de ciclo y pool, con el dato que las decide.
    Devuelve (filas, lectura); cada fila es (bloque, estado, condición, dato). Un ❌ es un veto: la
    combinación no se propone. Un ⚠️ es un aviso y no impide la propuesta. No añade ningún cálculo:
    ordena los que ya deciden `excluded`."""
    q, bt = o.quote, o.backtest
    rows = []
    rows.append(("Veto", {"favorable": OK, "precaución": WARN}.get(o.level, FAIL),
                 "La etapa del ciclo no es desfavorable", f"{_stage_line(rep, o.cycle)} → {o.level}"))
    rows.append(("Veto", OK if q.tvl is not None and q.tvl >= MIN_TVL else WARN,
                 "El pool tiene liquidez suficiente",
                 ("TVL desconocido" if q.tvl is None else f"TVL {q.tvl / 1e6:,.1f} M$")
                 + f" (mínimo {MIN_TVL / 1e6:,.0f} M$)"))
    if o.cycle == "1d" and o.pool.chain in NO_DAILY_ON:
        rows.append(("Veto", FAIL, "El gas de la red permite reajustar cada día",
                     f"en {o.pool.chain.capitalize()} no se propone el ciclo diario"))
    if o.fees_cycle is None or o.expected_result is None:
        rows.append(("Rentabilidad", FAIL, "Se puede calcular el resultado neto", o.excluded or "faltan datos"))
    else:
        il = o.expected_result * rep.capital
        rows.append(("Rentabilidad", OK if o.fees_cycle + il > 0 else FAIL,
                     "Las comisiones del ciclo superan el IL",
                     f"comisiones {_usd(o.fees_cycle)} frente a IL {_usd(il)}"))
        rows.append(("Rentabilidad", OK if o.net_cycle > 0 else FAIL,
                     "El neto es positivo después del gas",
                     f"neto {_usd(o.net_cycle)} por ciclo ({_pct(o.net_apr)} anual)"))
        rows.append(("Rentabilidad", OK if o.net_safe > 0 else FAIL,
                     f"Sigue positivo con un IL un {SAFETY_LOSS - 1:.0%} peor", f"neto {_usd(o.net_safe)} por ciclo"))
        if o.aave_apy is not None:
            rows.append(("Rentabilidad", OK if o.net_safe_apr > o.aave_apy else FAIL,
                         "Rinde más que depositar los mismos tokens en Aave",
                         f"neto con margen {_pct(o.net_safe_apr)} anual frente a {_apr2(o.aave_apy)} en Aave V3 ("
                         + ", ".join(f"{t} {_apr2(y)}" for t, y in o.aave_yields.items()) + ")"))
        share = o.gas_usd / o.fees_cycle if o.fees_cycle > 0 else math.inf
        rows.append(("Contexto", OK if share <= GAS_SHARE_WARN else WARN,
                     "El gas de un reajuste es pequeño frente a las comisiones",
                     f"gas {_usd(o.gas_usd)}" + (f", el {share:.0%} de las comisiones del ciclo"
                                                if math.isfinite(share) else "")))
    if q.fee_day is not None:
        rows.append(("Contexto", OK if q.fee_source == "real" else WARN,
                     "Las comisiones son las que pagó de verdad el pool",
                     "dato de la blockchain" if q.fee_source == "real" else "estimadas por volumen"))
    if bt.inside is not None:
        target = 2 * LP_QUANTILE - 1
        rows.append(("Contexto", OK if bt.inside >= target else WARN,
                     "En el pasado el precio se quedó dentro del rango",
                     f"{bt.inside:.0%} de {bt.samples} ciclos (el método apunta al {target:.0%})"))
    up = rep.asset.timeframes.get(HIGHER[o.cycle]) if HIGHER[o.cycle] else None
    if up is not None and up.status == "ok":
        against = {4} if rep.quote in STABLES else {2, 4}
        rows.append(("Contexto", WARN if up.stage in against else OK,
                     "El ciclo superior no empuja el precio fuera del rango",
                     f"{up.label.lower()} en etapa {up.stage} · {up.stage_name}"))
    r = rep.asset.timeframes[o.cycle]
    if r.status == "ok":
        rows.append(("Contexto", WARN if r.confidence_label == "baja" else OK,
                     "La lectura de la etapa es fiable", f"confianza {r.confidence_label}"))
    if o.vol_tvl is not None:
        rows.append(("Eficiencia", OK if o.vol_tvl > VOL_TVL_REF else WARN,
                     "El volumen diario supera el TVL del pool",
                     f"volumen {o.vol_tvl:.2f} veces el TVL (mediana de 30 días)"))
        rows.append(("Eficiencia", OK if o.fees_tvl >= FEES_TVL_REF else WARN,
                     f"Las comisiones del pool llegan al {FEES_TVL_REF:.1%} diario del TVL".replace(".", ","),
                     f"{o.fees_tvl:.3%} del TVL al día".replace(".", ",")))
    if o.stop_low:
        bt_txt = (f"; en el histórico saltó en el {bt.stop_hit:.0%} de los ciclos" if bt.stop_hit is not None else "")
        rows.append(("Riesgo", OK, "Stop-loss definido de antemano",
                     f"cerrar si baja de {_p(o.stop_low)} {rep.quote} ({_scen_txt(rep, o, 'stop')})" + bt_txt))
    rows.append(("Riesgo", OK if o.pool.dex in CONSOLIDATED_DEX else WARN,
                 "Protocolo consolidado (más de 1.000 M$ de TVL y años en producción)",
                 CONSOLIDATED_DEX.get(o.pool.dex, f"{o.pool.dex}: sin historial suficiente")))
    toks = o.pool.pair.split("/")
    if any(t in STABLE_TOKENS for t in toks):
        weak = [f"{t}: {STABLE_BACKING[t]}" for t in toks if t in STABLE_BACKING]
        rows.append(("Riesgo", WARN if weak else OK, "La estable tiene respaldo 1:1 en efectivo y letras del Tesoro",
                     "; ".join(weak) or "USDC: efectivo y letras del Tesoro a corto plazo"))
    custody = [f"{t} {TOKEN_CUSTODY[t]}" for t in toks if t in TOKEN_CUSTODY]
    rows.append(("Riesgo", WARN if custody else OK, "Los tokens no dependen de un custodio ni de un puente",
                 "; ".join(custody) or "ninguno"))
    if o.excluded and not any(st == FAIL for _, st, _, _ in rows):
        rows.append(("Veto", FAIL, "El programa puede evaluar esta combinación", o.excluded))
    fails = [c for _, st, c, _ in rows if st == FAIL]
    warns = sum(1 for _, st, _, _ in rows if st == WARN)
    if fails:
        reading = "**No cumple** las condiciones del método. Lo impide: " + "; ".join(x.lower() for x in fails) + "."
    else:
        reading = ("**Cumple** las condiciones del método"
                   + (f", con {warns} aviso{'s' if warns != 1 else ''} (⚠️)." if warns else "."))
    return rows, reading


def _checklist_md(rep: Report, o: Opportunity, title: str) -> list[str]:
    rows, reading = checklist(rep, o)
    return ([title, "", "| | Bloque | Condición | Dato |", "|---|---|---|---|"]
            + [f"| {st} | {block} | {cond} | {data} |" for block, st, cond, data in rows]
            + ["", f"Lectura: {reading}", ""])


def best_per_cycle(opps: list[Opportunity]) -> list[Opportunity]:
    """La combinación con mejor neto de cada ciclo (si ninguna tiene neto, la primera evaluada)."""
    out = []
    for key, _, _ in CYCLES:
        mine = [o for o in opps if o.cycle == key]
        if mine:
            out.append(max(mine, key=lambda o: (o.net_apr is not None, o.net_apr or 0)))
    return out


def _apr(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def _apr2(x: float | None) -> str:
    return "—" if x is None else f"{x:.2%}"


def headline(rep: Report) -> Opportunity | None:
    """La combinación que resume el análisis: la primera propuesta o, si no hay, la de mejor neto."""
    if rep.proposals:
        return rep.proposals[0]
    ok = [o for o in rep.opportunities if o.net_apr is not None]
    return max(ok, key=lambda o: o.net_apr) if ok else None


def _tokens(o: Opportunity) -> tuple[str, str]:
    toks = o.pool.pair.split("/")
    return (toks[0], toks[1]) if o.pool.base_is_token0 else (toks[1], toks[0])


def _windows_txt(o: Opportunity) -> str:
    w = o.apr_windows
    txt = " → ".join(f"{_apr(w[k])} ({k})" for k in ("24 h", "7 d", "30 d") if k in w)
    if "semana floja" in w:
        txt += f"; semana más floja de las últimas cuatro: {_apr(w['semana floja'])}"
    return txt


def _inflated(o: Opportunity) -> bool:
    w = o.apr_windows
    return "24 h" in w and "30 d" in w and w["30 d"] > 0 and w["24 h"] > INFLATED_24H * w["30 d"]


def _permanence_txt(rep: Report, o: Opportunity) -> str:
    pm = rep.permanence.get(o.cycle)
    if pm is None or pm.inside is None:
        return ""
    base_tok, quote_tok = _tokens(o)
    plural = {"1d": "diarios", "1w": "semanales", "1M": "mensuales"}[o.cycle]
    stay = (f"el precio aguantó dentro una mediana de **{pm.days_to_exit:.0f} días**"
            if pm.days_to_exit < HORIZON_DAYS else
            f"en más de la mitad el precio **no salió en {HORIZON_DAYS} días**")
    txt = (f"en los {pm.samples} rangos {plural} pasados, {stay}; sin tocarlo, el cierre quedó dentro el "
           f"**{pm.inside:.0%}** de los {HORIZON_DAYS} días siguientes")
    if pm.up is not None:
        txt += (f". Salió del rango en el {pm.exits:.0%} de los casos: el {pm.up:.0%} por arriba (te quedas en "
                f"{quote_tok}) y el {1 - pm.up:.0%} por abajo (te quedas en {base_tok})")
    return txt


def address_line(pool: P.Pool) -> str:
    """Dirección (en v4, identificador) del pool recomendado, con sus enlaces."""
    return (f"- **{'Identificador' if pool.is_v4 else 'Dirección'} del pool** ({pool.label}): `{pool.address}`"
            + (f" · Uniswap: {pool.app_url}" if pool.app_url else "") + f" · [GeckoTerminal]({pool.url})")


def short_answer(rep: Report) -> list[str]:
    """Respuesta corta: el pool que resume el análisis, sus comisiones por ventanas, cuánto aguanta
    el rango y el resultado sin tocar frente a reajustar. Solo ordena cifras ya calculadas."""
    o = headline(rep)
    if o is None:
        return ["## Respuesta corta", "",
                "- **Sin propuesta**: ninguna combinación de ciclo y pool se puede evaluar (falta liquidez o "
                "datos); ver los descartes más abajo.", ""]
    il = o.expected_result * rep.capital
    if rep.proposals:
        lines = [f"- **Mejor opción ahora: {o.pool.label}, ciclo {o.name.lower()}**. Neto estimado "
                 f"{_usd(o.net_cycle)} por ciclo ({_pct(o.net_apr)} anual), positivo incluso con la pérdida "
                 f"frente a mantener un {SAFETY_LOSS - 1:.0%} peor"
                 + (f" y por encima de depositar los mismos tokens en Aave V3 ({_apr2(o.aave_apy)} anual)."
                    if o.aave_apy is not None else "."), address_line(o.pool)]
    elif o.excluded == "resultado neto estimado negativo":
        lines = [f"- **Ahora no es rentable: mejor esperar.** El pool que menos pierde es **{o.pool.label}** "
                 f"(ciclo {o.name.lower()}): neto {_usd(o.net_cycle)} por ciclo ({_pct(o.net_apr)} anual), "
                 f"porque las comisiones ({_usd(o.fees_cycle)}) no cubren la pérdida frente a mantener ({_usd(il)})."]
    else:
        lines = [f"- **Ahora no se propone: mejor esperar.** La mejor combinación es **{o.pool.label}** (ciclo "
                 f"{o.name.lower()}, neto {_usd(o.net_cycle)} por ciclo, {_pct(o.net_apr)} anual), pero queda "
                 f"descartada: {o.excluded}."]
    if o.apr_windows:
        txt = (f"- **Comisiones con el precio dentro del rango** (APR sobre tu capital, lo que pagó de verdad "
               f"el pool): {_windows_txt(o)}.")
        if _inflated(o):
            txt += (" El dato de 24 h dobla con creces el de 30 días: es un día de volumen fuerte y no se "
                    "extrapola (el cálculo usa el menor entre 7 y 30 días).")
        lines.append(txt)
    perm = _permanence_txt(rep, o)
    if perm:
        lines.append(f"- **Rango {o.name.lower()}** ({_p(o.quote.low)} – {_p(o.quote.high)} {rep.quote}): "
                     + perm + ".")
    if o.passive_apr is not None:
        lines.append(f"- **Sin tocar {HORIZON_DAYS} días**: comisiones {_apr(o.passive_fee_apr)} anual; neto "
                     f"después de la pérdida frente a mantener y el gas, **{_pct(o.passive_apr)} anual**. "
                     f"**Reajustando cada ciclo**: comisiones {_apr(o.fee_apr_cycle)} anual; neto "
                     f"**{_pct(o.net_apr)} anual**.")
    if not rep.proposals:
        chain, y = idle_chain(rep), idle_yield(rep)
        if chain and y:
            lines.append(f"- **Alternativa sin riesgo de precio**: USDC depositado en Aave V3 en {chain.capitalize()} "
                         f"rinde hoy **{_apr2(y)} anual** (tipo variable).")
    from . import estrategia as E
    lines += E.short_line(rep, rep.hedges)
    lines.append("- El APR que muestran los agregadores solo cuenta comisiones; el neto descuenta además la "
                 "pérdida frente a mantener los tokens (IL), el gas y el cambio de tokens al reajustar.")
    return ["## Respuesta corta", "", *lines, ""]


def strategies_md(rep: Report) -> list[str]:
    """Tabla de comisiones por ventanas, permanencia en el rango y resultado sin tocar frente a
    reajustar, para la mejor combinación de cada ciclo."""
    best = [o for o in best_per_cycle(rep.opportunities) if o.fees_cycle is not None]
    if not best:
        return []
    out = ["## Comisiones, permanencia en el rango y estrategias", "",
           "| Ciclo | Pool | APR dentro: 24 h / 7 d / 30 d | Semana floja | Días dentro (mediana) | "
           f"Dentro sin tocar ({HORIZON_DAYS} d) | Salidas: arriba / abajo | Sin tocar: comisiones / neto | "
           "Reajustando: comisiones / neto |",
           "|---|---|---|---|---|---|---|---|---|"]
    for o in best:
        w, pm = o.apr_windows, rep.permanence.get(o.cycle) or Permanence(0)
        has = pm.inside is not None
        days = ("—" if not has else f"{pm.days_to_exit:.0f}" if pm.days_to_exit < HORIZON_DAYS
                else f"≥ {HORIZON_DAYS}")
        inside = f"{pm.inside:.0%}" if has else "—"
        exits = f"{pm.up:.0%} / {1 - pm.up:.0%}" if has and pm.up is not None else "—"
        out.append(f"| {o.name} | {o.pool.label} | {_apr(w.get('24 h'))} / {_apr(w.get('7 d'))} / "
                   f"{_apr(w.get('30 d'))} | {_apr(w.get('semana floja'))} | {days} | {inside} | {exits} | "
                   f"{_apr(o.passive_fee_apr)} / {_pct(o.passive_apr)} | {_apr(o.fee_apr_cycle)} / {_pct(o.net_apr)} |")
    out += ["", f"APR dentro: comisiones anualizadas sobre tu capital mientras el precio está en el rango, según "
                f"lo que pagó de verdad el pool en cada ventana. Días dentro, dentro sin tocar y salidas: el rango "
                f"del método abierto al cierre de cada ciclo pasado y dejado {HORIZON_DAYS} días sin tocar, sobre "
                f"velas diarias. Neto = comisiones + resultado frente a mantener − gas (y, reajustando, − cambio "
                f"de tokens), anualizado.", ""]
    return out


def render(rep: Report, footer: bool = True) -> str:
    if rep.error:
        return f"**No se puede analizar {rep.base}{'/' + rep.quote if rep.quote else ''}**: {rep.error}"
    b, q = rep.base, rep.quote
    out = [f"# Liquidez concentrada · {b}/{q} · capital {rep.capital:,.0f} $", "",
           f"Precio actual: **{_p(rep.price)} {q}**" + ("" if q in STABLES else f" (1 {q} = {_p(rep.quote_usd)} $)"),
           ""]
    out += [f"> {n}" for n in rep.warnings if n.startswith(("Se analiza como", "Par expresado"))] + (
        [""] if any(n.startswith(("Se analiza como", "Par expresado")) for n in rep.warnings) else [])
    out += short_answer(rep)
    if rep.proposals:
        for i, o in enumerate(rep.proposals, 1):
            out += _proposal(rep, o, i) + _checklist_md(rep, o, f"**Checklist de la propuesta {i}**")
    else:
        out += ["## Sin propuesta ahora",
                "Ninguna combinación de ciclo y pool cumple a la vez: etapa no desfavorable, liquidez "
                "suficiente y resultado neto positivo incluso con el margen de seguridad. Con los datos de "
                "hoy, las comisiones no compensan lo que se pierde frente a mantener los tokens: mejor "
                "esperar y volver a consultar.", ""]
        out += [idle_capital(rep), ""]
        best = best_per_cycle(rep.opportunities)
        if best:
            out += ["## Checklist: qué falla en la mejor combinación de cada ciclo", ""]
            for o in best:
                out += _checklist_md(rep, o, f"**Ciclo {o.name.lower()} · {o.pool.label}**")
    out += strategies_md(rep)
    from . import estrategia as E
    out += E.render(rep, rep.hedges)
    out += ["## Etapas del par", "",
            "| Ciclo | Etapa | Idoneidad | Rango del método | Histórico: dentro / resultado medio frente a mantener |",
            "|---|---|---|---|---|"]
    for key, name, _ in CYCLES:
        level, _ = rep.stages[key]
        rng, bt = rep.ranges[key], rep.backtests[key]
        rtxt = f"{_p(rng[0])} – {_p(rng[1])} ({rng[0] / rep.price - 1:+.1%} / {rng[1] / rep.price - 1:+.1%})" if rng else "—"
        btxt = (f"{bt.inside:.0%} / {bt.mean_result:+.2%} ({bt.samples} ciclos)" if bt.inside is not None
                else f"insuficiente ({bt.samples} ciclos)")
        out.append(f"| {name} | {_stage_line(rep, key)} | {level} | {rtxt} | {btxt} |")
    out += ["", "## Todas las combinaciones evaluadas", "",
            "| Ciclo | Pool | Comisiones/día | Fuente | Comisiones/ciclo | IL/ciclo | Gas/reajuste | "
            "Swap/ciclo | Neto/ciclo | Neto anual | Estado |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for o in sorted(rep.opportunities, key=lambda o: (o.net_apr is None, -(o.net_apr or 0))):
        star = "**propuesta**" if o in rep.proposals else (o.excluded or "válida")
        il = None if o.expected_result is None else o.expected_result * rep.capital
        out.append(f"| {o.name} | {o.pool.label} | {_usd(o.quote.fee_day)} | "
                   f"{ {'real': 'real', 'volumen': 'volumen'}.get(o.quote.fee_source, '—') } | "
                   f"{_usd(o.fees_cycle)} | {_usd(il)} | {_usd(o.gas_usd)} | {_usd(o.swap_cycle)} | "
                   f"{_usd(o.net_cycle)} | {_pct(o.net_apr)} | {star} |")
    if rep.illiquid:
        out += ["", f"Pools descartados por poca liquidez (TVL < {MIN_TVL / 1e3:,.0f} k$ o volumen < "
                    f"{MIN_VOLUME / 1e3:,.0f} k$/día): " + "; ".join(rep.illiquid)]
    if rep.warnings:
        out += ["", "Avisos: " + " · ".join(rep.warnings)]
    if footer:
        out += how_it_works()
    return "\n".join(out)


def how_it_works() -> list[str]:
    return ["", "**Cómo se calcula.** Rango: hasta dónde llegó el precio del par en el 90 % de los ciclos "
                "pasados, por cada lado, con la volatilidad actual. «Dentro» y «resultado medio» salen de "
                "aplicar el mismo método a los ciclos pasados sin ver el futuro; el resultado se reescala "
                "a la anchura actual del rango, que depende de la volatilidad de hoy. Comisiones «real»: lo "
                "que cobró de verdad cada unidad de liquidez de ese pool, leído de la blockchain (el menor "
                "entre la media de 7 y de 30 días), por la liquidez que aportas; «volumen»: estimación con la mediana del volumen "
                "de 30 días cuando no hay dato on-chain. Se cobran solo con el precio dentro. En la tabla, "
                "«Comisiones/ciclo» = comisiones por día × días del ciclo × fracción de ciclos dentro; «IL/ciclo» "
                "(pérdida impermanente) = resultado esperado frente a mantener × capital, casi siempre negativo; "
                "«Swap/ciclo» = cambio de tokens esperado al reajustar (la mitad de la posición, en los ciclos en "
                f"que el precio sale del rango, a la comisión del pool más barato de la red + {SWAP_SLIPPAGE:.2%} de "
                "deslizamiento); y Neto/ciclo = Comisiones/ciclo + IL/ciclo − Gas/reajuste − Swap/ciclo. Solo se "
                "propone lo que sigue siendo positivo con una pérdida un "
                f"{SAFETY_LOSS - 1:.0%} peor. Stop-loss: precio que el par no alcanzó en el {STOP_QUANTILE:.0%} de "
                f"los ciclos pasados, como mucho un {STOP_MAX_GAP:.0%} más allá del borde del rango. Checklist: ordena esas mismas condiciones; un ❌ es un veto (la "
                "combinación no se propone) y un ⚠️ es un aviso que no la impide (los criterios de eficiencia y de "
                "riesgo de tokens son avisos: las comisiones reales y el neto ya miden la rentabilidad). Que se cumplan todas indica "
                "que la combinación encaja en el método, no que vaya a salir bien. "
                "Son estimaciones: el volumen y la liquidez del pool cambian cada día.",
            "", f"_{DISCLAIMER}_"]


def _proposal(rep: Report, o: Opportunity, i: int) -> list[str]:
    b, q, qt = rep.base, rep.quote, o.quote
    pool = o.pool
    base_tok, quote_tok = (pool.pair.split("/") if pool.base_is_token0 else pool.pair.split("/")[::-1])
    level, reasons = rep.stages[o.cycle]
    return [
        f"## Propuesta {i}: ciclo {o.name.lower()} en {pool.label}",
        "",
        f"- **Pool**: {pool.dex} en {pool.chain.capitalize()}, par {pool.pair}, comisión "
        f"{P._pct(pool.fee)}" + (", sin hook" if pool.is_v4 else "") + ". "
        + ("Identificador" if pool.is_v4 else "Dirección") + f" `{pool.address}` "
        + f"([ver en GeckoTerminal]({pool.url}))."
        + (" Al crear la posición, comprueba que la aplicación marca **v4** y esta comisión; puedes "
           "depositar ETH sin envolver." if pool.is_v4 and "ETH" in pool.pair.split("/") else
           " Al crear la posición, comprueba que la aplicación marca **v4** y esta comisión."
           if pool.is_v4 else
           " Al crear la posición, elige **v3** (la aplicación propone v4 por defecto) y esta comisión."
           if pool.dex == "Uniswap v3" else ""),
        *([f"- **Abrir el pool en Uniswap**: {pool.app_url}"] if pool.app_url else []),
        f"- **Rango a introducir** (ya ajustado a los ticks del pool): mínimo **{_p(qt.low)}** y máximo "
        f"**{_p(qt.high)}** {q} por {b} ({qt.low / qt.pool_price - 1:+.1%} / {qt.high / qt.pool_price - 1:+.1%} "
        f"desde el precio del pool, {_p(qt.pool_price)}). Ticks {qt.tick_lower} / {qt.tick_upper}."
        + ("" if q in STABLES else f" Si el DEX muestra el precio al revés ({b} por {q}): mínimo "
           f"**{_p(1 / qt.high)}** y máximo **{_p(1 / qt.low)}**."),
        f"- **Depósito para {rep.capital:,.0f} $**: {_p(o.base_amount)} {base_tok} "
        f"({_pct(qt.asset_share, False)}) y {_p(o.quote_amount)} {quote_tok} ({_pct(1 - qt.asset_share, False)}).",
        f"- **Etapa**: {_stage_line(rep, o.cycle)} → {level}. " + " ".join([reasons[0].split(". ", 1)[-1]] + reasons[1:]),
        f"- **Histórico del método en este par** ({o.backtest.samples} ciclos): el precio no salió del "
        f"rango en el {o.backtest.inside:.0%} de los ciclos. Resultado esperado frente a mantener los "
        f"tokens, con la anchura actual del rango: {o.expected_result:+.2%} por ciclo "
        f"({o.expected_result * rep.capital:+,.2f} $).",
        f"- **Estimación por ciclo**: comisiones {_usd(o.fees_cycle)} ({_usd(qt.fee_day)}/día con el "
        f"precio dentro × {o.days:g} días × {o.backtest.inside:.0%}; "
        + ("según lo que pagó de verdad este pool, el menor entre los últimos 7 y 30 días"
           if qt.fee_source == "real"
           else "estimadas por volumen, sin dato on-chain")
        + "), resultado frente a mantener "
        f"{o.expected_result * rep.capital:+,.2f} $, gas {_usd(o.gas_usd)}, cambio de tokens al reajustar "
        f"{_usd(o.swap_cycle)} → **neto {_usd(o.net_cycle)}**, "
        f"equivalente a {_pct(o.net_apr)} anual. Con una pérdida un {SAFETY_LOSS - 1:.0%} peor: "
        f"{_usd(o.net_safe)} por ciclo.",
        f"- **Fondeo**: ten el capital en USDC en {pool.chain.capitalize()} hasta abrir. Para abrir hay que "
        f"cambiar unos {_usd(o.entry_swapped)} "
        f"{'a ' + base_tok if q in STABLES else 'a los dos tokens'}: coste estimado {_usd(o.entry_cost)}, "
        f"así que el neto del primer ciclo queda en {_usd(o.net_cycle - o.entry_cost)}.",
        *_risk_lines(rep, o, base_tok, quote_tok),
        f"- **Si sale por arriba** te quedas 100 % en {quote_tok}; **por abajo**, 100 % en {base_tok}. En "
        f"ambos casos deja de cobrar comisiones: vuelve a ejecutar la habilidad y reajusta. Revisa como "
        f"mínimo al cerrar cada vela {'diaria' if o.cycle == '1d' else 'semanal' if o.cycle == '1w' else 'mensual'}.",
        f"- **Seguimiento** una vez abierta: `python -m etapas.seguimiento {pool.chain} <número de la posición> "
        f"--capital {rep.capital:g} --precio {_p(qt.pool_price)}` compara las comisiones cobradas con lo que "
        f"ha variado la posición.",
        "",
    ]


def _risk_lines(rep: Report, o: Opportunity, base_tok: str, quote_tok: str) -> list[str]:
    """Escenarios frente a USDC (sin comisiones) y stop-loss."""
    if "min" not in o.scenarios:
        return []
    q, bt = rep.quote, o.backtest
    lines = [f"- **Escenarios sin comisiones**: si el precio toca el mínimo ({_p(o.scenarios['min'][0])}): "
             f"{_scen_txt(rep, o, 'min')}; si toca el máximo ({_p(o.scenarios['max'][0])}): "
             f"{_scen_txt(rep, o, 'max')}."]
    if "stop" in o.scenarios:
        txt = (f"- **Stop-loss**: cierra la posición si el precio baja de **{_p(o.stop_low)} {q}** "
               f"({_scen_txt(rep, o, 'stop')})")
        if "stop_alto" in o.scenarios:
            txt += (f" o si sube de **{_p(o.stop_high)} {q}** ({_scen_txt(rep, o, 'stop_alto')})")
        txt += (f". Es el precio que el par no alcanzó en el {STOP_QUANTILE:.0%} de los ciclos pasados: salta solo "
                f"ante un movimiento fuera de lo normal, no en cada salida del rango (como mucho, un "
                f"{STOP_MAX_GAP:.0%} más allá de su borde).")
        if bt.stop_hit is not None:
            txt += (f" En el histórico ({bt.samples} ciclos) saltó en el {bt.stop_hit:.0%}; resultado medio frente "
                    f"a mantener con stop {bt.mean_with_stop:+.2%} por ciclo y sin stop {bt.mean_result:+.2%}.")
        if q not in STABLES:
            txt += f" Si el DEX muestra el precio al revés ({rep.base} por {q}): " + (
                f"{_p(1 / o.stop_low)}" + (f" y {_p(1 / o.stop_high)}" if o.stop_high else "")) + "."
        txt += (" Uniswap no tiene stop-loss: ponlo como alerta de precio y cierra a mano, o usa una "
                "automatización de salida (por ejemplo, Auto-Exit de Revert Finance).")
        lines.append(txt)
    return lines


def idle_chain(rep: Report) -> str | None:
    """Red del pool mejor situado: donde conviene tener el capital parado."""
    ok = [o for o in rep.opportunities if o.net_apr is not None]
    return max(ok, key=lambda o: o.net_apr).pool.chain if ok else None


def idle_yield(rep: Report) -> float | None:
    """APY de depositar USDC en Aave V3 en la red del pool mejor situado (None si no se pudo leer)."""
    chain = idle_chain(rep)
    return A.supply_yield(chain, "USDC") if chain else None


def idle_capital(rep: Report) -> str:
    """Regla de capital parado cuando no hay propuesta."""
    chain = idle_chain(rep)
    where = chain.capitalize() if chain else None
    y = idle_yield(rep)
    return ("**Mientras tanto**: mantén el capital en USDC" + (f" en {where}, la red del pool mejor situado," if where else "")
            + " para no asumir la volatilidad de un token sin cobrar comisiones por ello y poder abrir la posición "
              "sin mover fondos de red cuando aparezca la oportunidad."
            + (f" Depositado en Aave V3 rinde hoy **{_apr2(y)} anual** (tipo variable), sin riesgo de precio; sí "
               f"de contrato inteligente y del respaldo de la estable." if y else ""))


def to_json(rep: Report) -> dict:
    from . import estrategia as E
    def opp(o: Opportunity) -> dict:
        rows, reading = checklist(rep, o)
        return {"checklist": [{"bloque": b, "estado": {OK: "cumple", WARN: "aviso", FAIL: "no cumple"}[st],
                               "condicion": c, "dato": d} for b, st, c, d in rows],
                "lectura": reading.replace("**", ""),
                "ciclo": o.name, "pool": o.pool.label, "direccion": o.pool.address, "red": o.pool.chain,
                "dex": o.pool.dex, "comision": o.pool.fee, "min": o.quote.low, "max": o.quote.high,
                "tick_inferior": o.quote.tick_lower, "tick_superior": o.quote.tick_upper,
                "precio_pool": o.quote.pool_price, "cantidad_base": o.base_amount,
                "cantidad_cotizada": o.quote_amount, "idoneidad": o.level,
                "historico_dentro": o.backtest.inside, "historico_resultado_medio": o.backtest.mean_result,
                "resultado_esperado": o.expected_result, "comisiones_ciclo": o.fees_cycle,
                "comisiones_dia": o.quote.fee_day, "fuente_comisiones": o.quote.fee_source or None,
                "enlace_dex": o.pool.app_url or None, "enlace_geckoterminal": o.pool.url,
                "gas_reajuste": o.gas_usd, "swap_ciclo": o.swap_cycle, "importe_a_cambiar": o.entry_swapped,
                "coste_entrada": o.entry_cost, "stop_bajo": o.stop_low, "stop_alto": o.stop_high,
                "escenarios": {k: {"precio": p, "resultado": r} for k, (p, r) in o.scenarios.items()},
                "historico_stop_salta": o.backtest.stop_hit, "historico_resultado_con_stop": o.backtest.mean_with_stop,
                "volumen_tvl": o.vol_tvl, "comisiones_tvl_dia": o.fees_tvl, "neto_ciclo": o.net_cycle,
                "neto_anual": o.net_apr, "neto_ciclo_con_margen": o.net_safe, "excluida": o.excluded or None,
                "apr_comisiones_dentro": o.apr_windows or None, "apr_comisiones_reajustando": o.fee_apr_cycle,
                "sin_tocar": None if o.passive_net is None else {
                    "dias": HORIZON_DAYS, "comisiones": o.passive_fees, "resultado_frente_a_mantener": o.passive_il,
                    "neto": o.passive_net, "apr_comisiones": o.passive_fee_apr, "neto_anual": o.passive_apr},
                "neto_anual_con_margen": o.net_safe_apr, "aave_apy_mismos_tokens": o.aave_apy,
                "aave_apy_por_token": o.aave_yields or None}
    perm = {name: {"rangos": pm.samples, "dias_dentro_mediana": pm.days_to_exit, "dentro_sin_tocar": pm.inside,
                   "salen": pm.exits, "salen_por_arriba": pm.up}
            for (key, name, _) in CYCLES if (pm := rep.permanence.get(key)) is not None}
    return {"par": f"{rep.base}/{rep.quote}", "capital": rep.capital, "precio": rep.price,
            "error": rep.error or None,
            "respuesta_corta": None if rep.error else "\n".join(short_answer(rep)[2:]).replace("**", "").strip(),
            "permanencia_en_rango": perm, "propuestas": [opp(o) for o in rep.proposals],
            "liquidez_cubierta": E.to_json(rep, rep.hedges) or None,
            "combinaciones": [opp(o) for o in rep.opportunities], "avisos": rep.warnings,
            "descartados_por_liquidez": rep.illiquid,
            "capital_parado": None if rep.proposals or rep.error else idle_capital(rep).replace("**", ""),
            "capital_parado_aave": None if rep.proposals or rep.error or not idle_chain(rep) else
            {"red": idle_chain(rep), "token": "USDC", "apy": idle_yield(rep)},
            "aviso": DISCLAIMER}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.oportunidad",
                                 description="Mejores rangos de liquidez concentrada para un par.")
    ap.add_argument("par", help="Par, por ejemplo ETH/USDC, BTC/USDT o ETH/BTC, o una temática: oro, "
                                "bitcoin, ether")
    ap.add_argument("--capital", type=float, default=5000.0, help="Capital por posición en dólares")
    ap.add_argument("--red", default="", metavar="REDES",
                    help="Limita a estas redes, separadas por comas: ethereum, base, arbitrum, solana")
    ap.add_argument("--json", action="store_true", help="Salida en JSON")
    ap.add_argument("--ltv", type=float, default=None, metavar="FRACCIÓN",
                    help="LTV de la liquidez cubierta (deuda / garantía en Aave), por defecto 0.35; como mucho "
                         "la mitad del LTV máximo de la garantía")
    args = ap.parse_args(argv)
    chains = [c.strip().lower() for c in args.red.split(",") if c.strip()]
    from .tema import analyze_theme, find_theme, render_theme, theme_json     # tema importa este módulo
    tema = find_theme(args.par)
    if tema is not None:
        trep = analyze_theme(tema, args.capital, chains or None)
        print(json.dumps(theme_json(trep), ensure_ascii=False, indent=2, default=float) if args.json
              else render_theme(trep))
        return 0
    rep = analyze(args.par, args.capital, chains or None, args.ltv)
    print(json.dumps(to_json(rep), ensure_ascii=False, indent=2, default=float) if args.json else render(rep))
    return 1 if rep.error else 0


if __name__ == "__main__":
    sys.exit(main())
