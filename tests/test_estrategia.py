"""Liquidez cubierta (estable en Aave V3 como garantía, activo prestado y aportado al pool): tamaño,
factor de salud, precio de liquidación, neto, vetos e integración en el análisis (sin red)."""
import numpy as np
import pandas as pd
import pytest

from etapas import aave as A
from etapas import estrategia as E
from etapas import oportunidad as op
from etapas import pools as P
from etapas.liquidez import position_value
from etapas.pools import FeeRates
from test_oportunidad import fake_world  # noqa: F401  (fixture)


def _res(token, chain="arbitrum", **kw):
    """Mercado de Aave V3 con valores parecidos a los reales de oct-2026."""
    vals = dict(supply_apr=0.0378, supply_apy=0.0385, borrow_apr=0.0448, borrow_apy=0.0458, utilization=0.9,
                supplied=1e9, borrowed=9e8, ltv=0.75, liquidation_threshold=0.78, liquidation_bonus=0.045,
                supply_cap=None, borrow_cap=None, debt_ceiling=0.0, collateral=True, borrowing=True,
                active=True, frozen=False, paused=False)
    if token == "WETH":
        vals.update(supply_apy=0.02, borrow_apy=0.025, ltv=0.8, liquidation_threshold=0.83,
                    liquidation_bonus=0.05, supplied=5e5, borrowed=4e5)
    vals.update(kw)
    return A.Reserve(chain, token, "0x1", **vals)


@pytest.fixture
def aave(monkeypatch):
    """Mercados falsos de Aave; se pueden cambiar por token con aave[token] = {...}."""
    over: dict = {}

    def reserve(chain, token):
        token = A.NATIVE_ALIAS.get(token, token)
        return _res(token, chain, **over.get(token, {}))
    monkeypatch.setattr(A, "reserve", reserve)
    monkeypatch.setattr(A, "supply_yield", lambda chain, token: 0.0)
    return over


@pytest.fixture
def rich(fake_world, monkeypatch):
    """Comisiones altas para que haya combinaciones rentables."""
    monkeypatch.setattr(P, "fee_rates", lambda pool, u0, u1: FeeRates(1e-12, 1e-12, 1e-12))
    return fake_world


# ---------------------------------------------------------------- piezas

def test_sizing_covers_the_asset_with_the_capital():
    value, deposit, debt = E.sizing(5000, 0.5, 0.35)
    assert debt == pytest.approx(0.5 * value)                     # el préstamo es el activo del pool
    assert debt / deposit == pytest.approx(0.35)                  # LTV usado
    assert deposit + (value - debt) == pytest.approx(5000)        # garantía + estable del pool = capital
    # HF = garantía × umbral / deuda: con el activo a p × umbral / LTV, HF = 1
    b = debt / 2000                                               # unidades a 2.000 $
    liq = E.liquidation_price(deposit, 0.78, b)
    assert liq == pytest.approx(2000 * 0.78 / 0.35)
    assert deposit * 0.78 / (b * liq) == pytest.approx(1)


def test_liquidation_history_counts_entries_that_reach_the_price():
    close = np.full(40, 100.0)
    high = close * 1.01
    high[20] = 250.0                                              # un día en que llega a 2,5 veces
    daily = pd.DataFrame({"open": close, "high": high, "low": close * 0.99, "close": close})
    hs = E.liquidation_history(daily, 2.0, horizon=5)
    assert hs.samples == 35
    assert hs.hits == 5                                           # entradas de los días 15 a 19
    assert hs.worst_rise == pytest.approx(1.5)
    assert E.liquidation_history(daily, 3.0, horizon=5).hits == 0
    assert E.liquidation_history(daily.iloc[:5], 2.0, horizon=5).samples == 0


# ---------------------------------------------------------------- en el análisis

def test_hedge_numbers_add_up(rich, aave):
    rep = op.analyze("ETH/USDC", 5000)
    calc = [h for h in rep.hedges if h.net_cycle is not None]
    assert calc and len(rep.hedges) == len(rep.opportunities)
    for h in calc:
        o = h.opp
        assert (h.collateral, h.debt) == ("USDC", "WETH")
        assert h.ltv_used == pytest.approx(0.35) and h.ltv_max == 0.75
        assert h.health == pytest.approx(0.78 / 0.35)
        assert h.deposit + h.lp_stable == pytest.approx(5000)
        assert h.borrow_usd / h.deposit == pytest.approx(0.35)
        assert h.borrow_amount * o.quote.pool_price == pytest.approx(h.borrow_usd)
        assert h.liq_price == pytest.approx(o.quote.pool_price * h.health)
        scale = h.lp_value / 5000
        assert h.fees_cycle == pytest.approx(o.fees_cycle * scale)
        assert h.il_cycle == pytest.approx(o.expected_result * h.lp_value)
        assert h.interest_cycle == pytest.approx((h.borrow_usd * 0.025 - h.deposit * 0.0385) * o.days / 365)
        assert h.gas_cycle > o.gas_usd and h.entry_gas > 0
        assert h.net_cycle == pytest.approx(h.fees_cycle + h.il_cycle - h.interest_cycle - h.gas_cycle - h.swap_cycle)
        assert h.net_safe == pytest.approx(h.net_cycle + (op.SAFETY_LOSS - 1) * h.il_cycle)
        assert h.history is not None and h.history.samples > 0
        # Cubierta: en el precio de entrada el resultado sin comisiones es 0; fuera del rango se pierde
        p0 = o.quote.pool_price
        lp = h.lp_value * position_value(o.quote.low, o.quote.low, o.quote.high) / position_value(p0, o.quote.low, o.quote.high)
        assert h.scenarios["min"][1] == pytest.approx((lp + h.deposit - h.borrow_amount * o.quote.low) / 5000 - 1)
        assert h.scenarios["min"][1] < 0 and h.scenarios["max"][1] < 0
        assert h.scenarios["liquidacion"][1] < h.scenarios["max"][1]
    for h in rep.hedges:
        if not h.excluded:
            assert h.net_safe_apr > h.supply_apy and h.health >= E.MIN_HEALTH


def test_ltv_is_capped_at_half_the_maximum(rich, aave):
    rep = op.analyze("ETH/USDC", 5000, hedge_ltv=0.6)
    h = next(h for h in rep.hedges if h.net_cycle is not None)
    assert rep.hedge_ltv == 0.6 and h.ltv_used == pytest.approx(0.375)


@pytest.mark.parametrize("token, change, reason", [
    ("USDC", {"debt_ceiling": 2e6}, "Modo de Aislamiento"),
    ("USDC", {"ltv": 0.0}, "LTV 0"),
    ("USDC", {"supply_cap": 1e9 + 100}, "límite de depósito"),
    ("WETH", {"borrow_cap": 4e5 + 0.01}, "límite de deuda"),
    ("WETH", {"frozen": True}, "no se puede pedir prestado"),
    ("USDC", {"liquidation_threshold": 0.6}, "factor de salud inicial"),
    ("USDC", {"supply_apy": 1e6}, "rinde menos que depositar el USDC en Aave"),
])
def test_vetoes(rich, aave, token, change, reason):
    aave[token] = change
    rep = op.analyze("ETH/USDC", 5000)
    calc = [h for h in rep.hedges if h.opp.fees_cycle is not None]
    assert calc and all(reason in h.excluded for h in calc)


def test_without_aave_data_it_is_excluded_not_invented(fake_world):
    rep = op.analyze("ETH/USDC", 5000)                            # sin red: Aave no responde
    assert rep.hedges and all(h.net_cycle is None and h.excluded for h in rep.hedges)
    assert any("sin datos de Aave" in h.excluded for h in rep.hedges)
    assert "Liquidez cubierta" in op.render(rep)


def test_not_for_two_volatile_tokens(rich, aave):
    rep = op.analyze("ETH/BTC", 5000)
    assert rep.hedges == [] and "Liquidez cubierta" not in op.render(rep)
    assert op.to_json(rep)["liquidez_cubierta"] is None


def test_output_section_and_json(rich, aave):
    rep = op.analyze("ETH/USDC", 5000)
    text = op.render(rep)
    assert "## Liquidez cubierta: USDC en Aave V3 como garantía y ETH prestado" in text
    assert "Factor de salud inicial 2.23" in text and "Liquidación si ETH sube a" in text
    assert "**Liquidez cubierta**" in "\n".join(op.short_answer(rep))
    data = op.to_json(rep)["liquidez_cubierta"]
    assert len(data) == len(rep.hedges)
    one = next(d for d in data if d["neto_ciclo"] is not None)
    assert one["factor_salud"] == pytest.approx(0.78 / 0.35) and one["historico_liquidacion"]["dias_entrada"] > 0
    assert one["direccion"] == next(h for h in rep.hedges if h.net_cycle is not None).opp.pool.address


def test_recommended_pool_address_is_shown(rich, aave):
    from etapas import cribado as C
    rep = op.analyze("ETH/USDC", 5000)
    assert rep.proposals
    addr = op.headline(rep).pool.address
    assert f"**Dirección del pool** ({op.headline(rep).pool.label}): `{addr}`" in "\n".join(op.short_answer(rep))
    hedge = E.best(rep.hedges)
    if not hedge.excluded:
        assert f"`{hedge.opp.pool.address}`" in "\n".join(E.render(rep, rep.hedges))
    s = C.Screen(5000, ["arbitrum"], 0.001, reports={"ETH/USDC": rep})
    text = C.render(s)
    assert "**Direcciones de los pools recomendados**" in text
    assert all(f"`{o.pool.address}`" in text for o in rep.proposals)


def test_cli_ltv(rich, aave, capsys):
    assert op.main(["ETH/USDC", "--ltv", "0.25"]) == 0
    assert "LTV usado 25%" in capsys.readouterr().out
