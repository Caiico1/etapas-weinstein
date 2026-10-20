"""Posición en Aave: factor de salud y distancia a la liquidación con posiciones conocidas (sin red)."""
import pytest

from etapas import aave_posicion as AP


def _h(symbol, sup_usd=0.0, debt_usd=0.0, lt=0.83, collateral=True):
    return AP.Holding(symbol, sup_usd, sup_usd, debt_usd, debt_usd, 1.0, collateral, lt, 0.0, 0.0)


def test_eth_de_garantia_y_usdc_de_deuda():
    hs = [_h("WETH", sup_usd=10_000), _h("USDC", debt_usd=10_000 * 0.83 / 1.8)]
    assert AP._hf(hs) == pytest.approx(1.8)
    fall = AP._solve(hs, -0.999, 0.0)
    assert -fall == pytest.approx(1 - 1 / 1.8)                    # cae un 44,4 % → HF 1
    assert AP._solve(hs, 0.0, 20.0) is None                       # si sube, nunca se liquida


def test_corto_se_liquida_si_el_activo_sube():
    hs = [_h("USDC", sup_usd=10_000, lt=0.78), _h("WETH", debt_usd=10_000 * 0.78 / 2.0)]
    assert AP._solve(hs, 0.0, 20.0) == pytest.approx(1.0)        # sube un 100 % → HF 1
    assert AP._solve(hs, -0.999, 0.0) is None


def test_un_deposito_que_no_es_garantia_no_cuenta():
    hs = [_h("WETH", sup_usd=10_000), _h("cbBTC", sup_usd=10_000, collateral=False), _h("USDC", debt_usd=4_150)]
    assert AP._hf(hs) == pytest.approx(10_000 * 0.83 / 4_150)


def test_formato_espanol():
    assert AP._num(1656.8969, 2) == "1.656,90"
