"""Backtest de préstamo con garantía en Aave: matemática de la simulación con series sintéticas (sin BD)."""
import pandas as pd
import pytest

from etapas import aave_backtest as A
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


def test_hf_por_debajo_del_ltv_no_se_puede_abrir():
    with pytest.raises(BacktestError):
        A.simulate(_series([2000.0] * 48), A.Loan(1.0))         # 0,83 / 1,0 > LTV 0,80
