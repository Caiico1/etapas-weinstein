"""Backtest de Aave (préstamo con garantía y liquidez cubierta): matemática con series sintéticas (sin BD)."""
import math

import pandas as pd
import pytest

from etapas import aave_backtest as A
from etapas import backtest as B
from etapas.backtest import BacktestError


def _series(prices, lt=0.83, ltv=0.80, bonus=0.05, borrow_growth=0.0, supply_growth=0.0, coll_growth=0.0):
    """Horas con el precio de la garantía dado (la deuda vale 1 $); *_growth = interés por hora."""
    n = len(prices)
    g = lambda r: [(1 + r) ** i for i in range(n)]
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=n, freq="h"),
                         "c_index": g(coll_growth), "c_price": prices, "ltv": [ltv] * n, "lt": [lt] * n,
                         "bonus": [bonus] * n, "d_index": g(borrow_growth), "d_supply_index": g(supply_growth),
                         "d_price": [1.0] * n, "can_borrow": [True] * n})


def test_precio_quieto_sin_intereses_no_cambia_nada():
    r = A.simulate(_series([2000.0] * 48), A.Loan(1.8, gas_usd=0), capital=10_000)
    assert r.debt_usd == pytest.approx(10_000 * 0.83 / 1.8)
    assert r.hf_min == pytest.approx(1.8)
    assert r.hf_needed == pytest.approx(1.0)
    assert r.liquidations == 0 and r.interest_usd == 0
    assert r.final_usd == pytest.approx(10_000) and r.vs_hold_pct == pytest.approx(0)


def test_caida_que_no_liquida_y_hf_necesario():
    r = A.simulate(_series([2000.0] * 10 + [1200.0] * 10), A.Loan(1.8, gas_usd=0))
    assert r.liquidations == 0
    assert r.hf_min == pytest.approx(1.8 * 0.6)
    assert r.hf_needed == pytest.approx(1 / 0.6)


def test_liquidacion_total_por_debajo_de_095():
    r = A.simulate(_series([2000.0] * 10 + [1000.0] * 10), A.Loan(1.8, gas_usd=0))
    debt = 10_000 * 0.83 / 1.8                                   # HF cae a 0,9 → se liquida toda la deuda
    assert r.liquidations == 1
    assert r.liquidated_usd == pytest.approx(debt)
    assert r.liquidation_loss_usd == pytest.approx(debt * 0.05)
    coll_left = 5_000 - debt * 1.05                              # la garantía vale 5.000 $ tras la caída
    assert r.final_usd == pytest.approx(coll_left + debt)        # + lo prestado, que sigue depositado
    assert r.final_usd - r.hold_usd == pytest.approx(-debt * 0.05)


def test_liquidacion_parcial_paga_la_mitad():
    p = 2000.0 * 0.97 / 1.8                                      # HF justo 0,97: entre 0,95 y 1
    r = A.simulate(_series([2000.0] * 5 + [p] * 5), A.Loan(1.8, gas_usd=0), capital=100_000)
    debt = 100_000 * 0.83 / 1.8
    assert r.liquidations == 1                                   # tras pagar la mitad, HF > 1
    assert r.liquidated_usd == pytest.approx(debt / 2)
    assert r.hf_min > 1


def test_intereses_y_coste_anual():
    hourly = 0.05 / A.YEAR_H                                     # 5 % anual simple, por hora
    r = A.simulate(_series([2000.0] * (A.YEAR_H + 1), borrow_growth=hourly, supply_growth=hourly * 0.6),
                   A.Loan(1.8, gas_usd=0))
    assert r.borrow_apr == pytest.approx(0.05, rel=0.03)
    assert r.net_cost_apr == pytest.approx(0.02, rel=0.05)
    assert r.final_usd - r.hold_usd == pytest.approx(-(r.interest_usd - r.carry_usd), rel=1e-6)


def test_defender_devuelve_deuda_hasta_el_hf_inicial():
    r = A.simulate(_series([2000.0] * 5 + [1300.0] * 5), A.Loan(1.8, "defender", 1.3, gas_usd=0))
    debt = 10_000 * 0.83 / 1.8                                   # HF 1,17 < 1,3 → defensa
    target = 6_500 * 0.83 / 1.8
    assert r.defenses == 1 and r.liquidations == 0
    assert r.added_usd == pytest.approx(debt - target)
    assert r.hf_min == pytest.approx(1.8)


def test_con_ltv_0_no_se_abre_y_las_ventanas_lo_cuentan():
    df = _series([2000.0] * 48)
    df.loc[:23, "ltv"] = 0.0                                     # reserva congelada el primer día
    with pytest.raises(A.CannotOpen):
        A.simulate(df, A.Loan(1.8))
    assert A._try(df, A.Loan(1.8), 10_000) is None
    s = A.summarize([None, A.simulate(df.iloc[24:].reset_index(drop=True), A.Loan(1.8))], A.Loan(1.8))
    assert s["ventanas"] == 1 and s["sin_abrir"] == 1


def test_tras_liquidar_lo_prestado_no_abarata_el_prestamo():
    hourly = 0.05 / A.YEAR_H
    r = A.simulate(_series([2000.0] * 10 + [1000.0] * 2000, borrow_growth=hourly, supply_growth=hourly * 0.6),
                   A.Loan(1.8, gas_usd=0))
    assert r.liquidations == 1
    assert r.net_cost_apr > 0                                    # el depósito de lo prestado ya no cuenta


# --- liquidez cubierta: pool WETH/USDC sintético + USDC de garantía y WETH prestado --------------

POOL ={"chain": "base", "version": "v3", "pool_id": "0x0", "label": "WETH/USDC", "fee": 500, "tick_spacing": 10,
        "symbol0": "WETH", "symbol1": "USDC", "dec0": 18, "dec1": 6, "asset_is_token0": True}
T0 = round(math.log(2000 / 1e12) / math.log(B.TICK_BASE))       # tick de ETH ≈ 2000 USDC


def _covered(ticks, lt=0.78, ltv=0.75):
    n = len(ticks)
    eth = [B.TICK_BASE ** t * 1e12 for t in ticks]
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=n, freq="h"), "tick": ticks,
                         "sqrt_price": [B.TICK_BASE ** (t / 2) for t in ticks], "usd0": eth, "usd1": [1.0] * n,
                         "fee_growth0": [0.0] * n, "fee_growth1": [0.0] * n, "lp_fee": [500] * n,
                         "c_index": [1.0] * n, "c_price": [1.0] * n, "ltv": [ltv] * n, "lt": [lt] * n,
                         "bonus": [0.05] * n, "d_index": [1.0] * n, "d_supply_index": [1.0] * n,
                         "d_price": eth, "can_borrow": [True] * n})


def test_cubierta_tamanos_y_precio_quieto():
    df = _covered([T0] * 48)
    cv = A.Covered(0.10, 2.0, gas_usd=0)
    r = A.simulate_covered(POOL, df, cv, 10_000)
    s = A.asset_share(POOL, df, 0.10)
    assert 0.4 < s < 0.6
    assert r.deposit + (1 - s) * r.lp_value == pytest.approx(10_000)        # garantía + estable del pool = capital
    assert r.debt_usd == pytest.approx(s * r.lp_value)                       # se pide justo el activo del pool
    assert r.debt_usd / r.deposit == pytest.approx(0.78 / 2.0)
    assert r.hf_min == pytest.approx(2.0)
    assert r.final_usd == pytest.approx(10_000, rel=1e-3)                    # solo el cambio de tokens del pool


def test_cubierta_neutraliza_la_subida_dentro_del_rango():
    up = round(math.log(1.05) / math.log(B.TICK_BASE))                       # ETH +5 %, dentro de ±10 %
    df = _covered([T0] * 10 + [T0 + up] * 10)
    r = A.simulate_covered(POOL, df, A.Covered(0.10, 2.0, gas_usd=0), 10_000)
    assert abs(r.pnl_pct) < 0.005                                            # solo la pérdida frente a mantener
    assert r.vs_unhedged_pct < -0.01                                         # el pool sin cubrir sí gana con la subida
    assert r.hf_min == pytest.approx(2.0 / 1.05, rel=1e-3)
    assert r.liquidations == 0


def test_cubierta_se_liquida_si_el_activo_sube_mas_que_el_hf():
    up = round(math.log(2.2) / math.log(B.TICK_BASE))                        # ETH ×2,2 con HF 2
    r = A.simulate_covered(POOL, _covered([T0] * 10 + [T0 + up] * 10), A.Covered(0.10, 2.0, gas_usd=0), 10_000)
    assert r.liquidations >= 1
    assert r.hf_needed == pytest.approx(2.2, rel=1e-3)


# --- carry en E-Mode: wstETH (staking en el precio) de garantía, WETH prestado ------------------

def _carry(hours, staking=0.03, borrow=0.02, lt=0.95, ltv=0.93, emode="wstETH/WETH"):
    """ETH quieto a 2000 $; wstETH sube con el staking (simple por hora); la deuda crece con `borrow`."""
    h = range(hours)
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=hours, freq="h"),
                         "c_index": [1.0] * hours, "c_price": [2400 * (1 + staking * i / A.YEAR_H) for i in h],
                         "ltv": [ltv] * hours, "lt": [lt] * hours, "bonus": [0.01] * hours,
                         "d_index": [1 + borrow * i / A.YEAR_H for i in h], "d_supply_index": [1.0] * hours,
                         "d_price": [2000.0] * hours, "can_borrow": [True] * hours, "emode": [emode] * hours})


def test_carry_apalancamiento_y_rendimiento_en_eth():
    r = A.simulate_carry(_carry(A.YEAR_H + 1), A.Carry(1.05, swap_bps=0, gas_usd=0), 10_000)
    x = 1.05 / (1.05 - 0.95)                                     # 10,5x
    assert r.leverage == pytest.approx(x)
    assert r.staking_apr == pytest.approx(0.03, rel=1e-3)
    assert r.plain_apr == pytest.approx(0.03, rel=1e-3)
    assert r.eth_apr == pytest.approx(0.03 * x - 0.02 * (x - 1), rel=0.01)   # ≈ 12,5 %
    assert r.liquidations == 0


def test_carry_negativo_si_el_prestamo_cuesta_mas_que_el_staking():
    r = A.simulate_carry(_carry(A.YEAR_H + 1, borrow=0.05), A.Carry(1.05, swap_bps=0, gas_usd=0), 10_000)
    assert r.eth_apr < 0
    assert r.hf_min < 1.05                                       # el HF cae despacio con el carry negativo


def test_carry_sin_emode_no_se_abre():
    with pytest.raises(A.CannotOpen):
        A.simulate_carry(_carry(48, emode=""), A.Carry(1.05))


def test_hf_por_debajo_del_ltv_no_se_puede_abrir():
    with pytest.raises(BacktestError):
        A.simulate(_series([2000.0] * 48), A.Loan(1.0))         # 0,83 / 1,0 > LTV 0,80
