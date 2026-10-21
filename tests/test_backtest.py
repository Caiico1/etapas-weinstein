"""Backtest de liquidez contra MetaProDB: matemática de la simulación con series sintéticas (sin BD)."""
import math

import pandas as pd
import pytest

from etapas import backtest as B

POOL = {"chain": "ethereum", "version": "v3", "pool_id": "0x0", "label": "WETH/USDC", "fee": 500,
        "tick_spacing": 10, "symbol0": "WETH", "symbol1": "USDC", "dec0": 18, "dec1": 6}


def _series(ticks, growth0=0.0, growth1=0.0, eth=2000.0):
    """Horas con el tick dado; precio humano de WETH = 1,0001^tick × 10^12 (USDC por WETH)."""
    n = len(ticks)
    sp = [B.TICK_BASE ** (t / 2) for t in ticks]
    usd0 = [B.TICK_BASE ** t * 1e12 for t in ticks]
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=n, freq="h"), "tick": ticks,
                         "sqrt_price": sp, "usd0": usd0, "usd1": [1.0] * n,
                         "fee_growth0": [growth0] * n, "fee_growth1": [growth1] * n, "lp_fee": [500] * n})


T0 = round(math.log(2000 / 1e12) / math.log(B.TICK_BASE))       # tick de ETH ≈ 2000 USDC


def test_precio_quieto_sin_comisiones_conserva_el_capital_menos_costes():
    r = B.simulate(POOL, _series([T0] * 48), B.Strategy(0.10, gas_usd=0), capital=10_000)
    assert r.in_range_pct == pytest.approx(1.0)
    assert r.fees_usd == 0
    assert r.final == pytest.approx(10_000 - r.costs_usd, rel=1e-9)
    assert r.costs_usd == pytest.approx(10_000 * 0.0005 * 0.5, rel=0.05)   # compra de ~la mitad en WETH


def test_comisiones_son_liquidez_por_fee_growth():
    g1 = 1e-6                                                             # USDC (unidades mínimas) por unidad de L y hora
    df = _series([T0] * 25, growth1=g1)
    st = B.Strategy(0.10, gas_usd=0)
    r = B.simulate(POOL, df, st, capital=10_000)
    tl, tu = B._range(T0, 0.10, 10)
    a0, a1 = B._amounts(df["sqrt_price"][0], B._sqrt(tl), B._sqrt(tu))
    per_l = a0 / 1e18 * df["usd0"][0] + a1 / 1e6
    L = (10_000 - r.costs_usd) / per_l
    assert r.fees_usd == pytest.approx(L * g1 * 24 / 1e6, rel=1e-6)


def test_fuera_de_rango_no_cobra_y_queda_todo_en_un_token():
    up = T0 + 3000                                                        # +35 %: fuera de ±10 %
    df = _series([T0] + [up] * 30, growth1=1e-6)
    r = B.simulate(POOL, df, B.Strategy(0.10, gas_usd=0), capital=10_000)
    assert r.in_range_pct < 0.05                                          # solo media hora de la primera
    assert r.vs_token1_pct > 0                                            # vendió WETH al subir: más USDC que al principio
    assert r.vs_hodl_pct < 0                                              # pero menos que mantener (IL)


def test_reajuste_recentra_y_vuelve_a_cobrar():
    up = T0 + 3000
    df = _series([T0] + [up] * 30, growth1=1e-6)
    r = B.simulate(POOL, df, B.Strategy(0.10, rebalance="fuera", wait_hours=0, gas_usd=1), capital=10_000)
    assert r.rebalances == 1
    assert r.in_range_pct > 0.9
    assert r.fees_usd > 0


def _cycle(df, low, high, stop_low=None, level="apta"):
    return B.Cycle(df["hour"].iloc[0], df["hour"].iloc[-1], low, high, stop_low, None, level)


def test_metodo_un_ciclo_cobra_como_un_rango_fijo():
    df = _series([T0] * 25, growth1=1e-6)
    m = B.Method("1w", gas_usd=0)
    r = B.simulate_method(POOL, df, [_cycle(df, 1800, 2200)], m, capital=10_000)
    assert (r.cycles, r.vetoed, r.stops) == (1, 0, 0)
    assert r.in_range_pct == pytest.approx(1.0)
    assert r.fees_usd > 0
    # Sin moverse el precio, el resultado es lo cobrado menos el coste de comprar la parte en WETH
    assert r.final == pytest.approx(10_000 + r.fees_usd - r.costs_usd, rel=1e-6)


def test_metodo_veto_de_etapa_se_queda_en_la_estable():
    df = _series([T0] + [T0 + 2000] * 24, growth1=1e-6)                  # ETH sube ~22 %
    r = B.simulate_method(POOL, df, [_cycle(df, 1800, 2200, level="sin datos")], B.Method("1w", gas_usd=0))
    assert (r.cycles, r.vetoed) == (0, 1)
    assert r.fees_usd == 0
    assert r.final == pytest.approx(10_000)                                # todo el tiempo en USDC
    r2 = B.simulate_method(POOL, df, [_cycle(df, 1800, 2200, level="sin datos")],
                           B.Method("1w", gas_usd=0, use_stage=False))
    assert r2.cycles == 1                                                  # sin veto, abre


def test_metodo_stop_cierra_y_espera_en_la_estable():
    down = T0 - 2000                                                       # ETH ≈ 1637
    df = _series([T0] + [down] * 24)
    r = B.simulate_method(POOL, df, [_cycle(df, 1800, 2200, stop_low=1700)], B.Method("1w", gas_usd=0))
    assert r.stops == 1
    r_no = B.simulate_method(POOL, df, [_cycle(df, 1800, 2200, stop_low=1700)],
                             B.Method("1w", gas_usd=0, use_stop=False))
    assert r_no.stops == 0
    assert r.final == pytest.approx(r_no.final * (1 - 0.0005), rel=1e-3)  # mismo valor menos la venta (precio quieto tras el stop)


def test_metodo_stop_en_el_borde_cierra_al_salir_del_rango():
    df = _series([T0] + [T0 - 1335] * 24)                                 # ETH ≈ 1750: fuera del rango, sin llegar al stop
    cy = [_cycle(df, 1800, 2200, stop_low=1700)]
    m = B.Method("1w", gas_usd=0, stop_at_edge=True)
    assert m.name == "método semanal stop en el borde"
    assert B.simulate_method(POOL, df, cy, B.Method("1w", gas_usd=0)).stops == 0
    assert B.simulate_method(POOL, df, cy, m).stops == 1


def test_stop_cierra_a_estable():
    down = T0 - 4000                                                      # −33 %
    df = _series([T0] + [down] * 10)
    r = B.simulate(POOL, df, B.Strategy(0.10, stop=0.15, gas_usd=0), capital=10_000)
    assert r.stopped
    assert r.final < 10_000 * 0.85
