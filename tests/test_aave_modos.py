"""Préstamo con cambio de modo (aave_modos): matemática con series sintéticas (sin BD ni Binance)."""
import math

import pandas as pd
import pytest

from etapas import aave_backtest as A
from etapas import aave_modos as M
from etapas.classifier import BREAK_FRAC

CAP, DEBT = 11_500.0, 3_450.0


def _series(prices, stages=None, lt=0.78, bonus=0.05, a_borrow=0.0, s_supply=0.0, s_borrow=0.0, can_a=True):
    """Horas con el precio de A dado (S vale 1 $); stages = etapa semanal de cada hora; *_borrow/_supply =
    interés por hora."""
    n = len(prices)
    g = lambda r: [(1 + r) ** i for i in range(n)]
    st = stages if stages is not None else [2.0] * n
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=n, freq="h"),
                         "a_sidx": [1.0] * n, "a_bidx": g(a_borrow), "a_px": prices, "a_lt": [lt] * n,
                         "a_bonus": [bonus] * n, "a_can_borrow": [can_a] * n,
                         "s_sidx": g(s_supply), "s_bidx": g(s_borrow), "s_px": [1.0] * n, "s_lt": [lt] * n,
                         "s_bonus": [bonus] * n, "s_can_borrow": [True] * n,
                         "st_w": st, "st_d": st, "nivel": [None] * n})


def _plan(rule, **kw):
    kw = {"gas_usd": 0.0, "coll_bps": 0.0, "debt_bps": 0.0, **kw}
    return M.Plan(rule, CAP, DEBT, **kw)


def test_precio_quieto_sin_intereses_no_cambia_nada():
    for rule in ("siempre-alcista", "siempre-mixto", "siempre-neutral", "siempre-bajista"):
        r = M.simulate(_series([80_000.0] * 48), _plan(rule))
        assert r.equity_end == pytest.approx(CAP - DEBT)
        assert r.amortized == pytest.approx(0)
        assert r.hf_min == pytest.approx(CAP * 0.78 / DEBT)
        assert r.switches == 0 and r.liquidations == 0


def test_siempre_alcista_coincide_con_aave_backtest():
    prices = [80_000.0 * (1 + 0.3 * math.sin(i / 40)) for i in range(500)]
    df = _series(prices, s_borrow=0.05 / A.YEAR_H)
    r = M.simulate(df, _plan("siempre-alcista"))
    old = pd.DataFrame({"hour": df["hour"], "c_index": df["a_sidx"], "c_price": df["a_px"], "ltv": 0.73,
                        "lt": df["a_lt"], "bonus": df["a_bonus"], "d_index": df["s_bidx"],
                        "d_supply_index": df["s_sidx"], "d_price": df["s_px"], "can_borrow": True})
    l = A.simulate(old, A.Loan(CAP * 0.78 / DEBT, gas_usd=0), CAP, keep_proceeds=False)
    assert r.equity_end == pytest.approx(l.final_usd)
    assert r.hf_min == pytest.approx(l.hf_min)


def test_bajista_amortiza_lo_que_cae_el_activo():
    r = M.simulate(_series([80_000.0] * 10 + [56_000.0] * 10), _plan("siempre-bajista"))
    assert r.debt_end_usd == pytest.approx(DEBT * 0.7)
    assert r.amortized == pytest.approx(0.3)


def test_mixto_tiene_la_mitad_de_exposicion():
    r = M.simulate(_series([80_000.0] * 10 + [96_000.0] * 10), _plan("siempre-mixto"))
    assert r.amortized == pytest.approx(CAP * 0.5 * 0.2 / DEBT)
    a = M.simulate(_series([80_000.0] * 10 + [96_000.0] * 10), _plan("siempre-alcista"))
    assert a.amortized == pytest.approx(2 * r.amortized)


def test_cambio_a_bajista_en_la_hora_de_la_senal_y_con_costes():
    prices = [80_000.0] * 10 + [60_000.0] * 10 + [48_000.0] * 10
    stages = [2.0] * 10 + [4.0] * 20                      # la etapa 4 se conoce cuando cae a 60.000
    r = M.simulate(_series(prices, stages), _plan("semanal", coll_bps=30, debt_bps=5))
    coll = CAP * 0.75                                     # garantía en A al cambiar
    coll_after = coll * (1 - 0.003)                       # Collateral Swap: pierde 30 pb
    debt_after = DEBT * 1.0005                            # Debt Swap: la deuda sube 5 pb
    assert r.switches == 1
    assert r.switch_cost_usd == pytest.approx(coll * 0.003 + DEBT * 0.0005)
    assert r.debt_end_usd == pytest.approx(debt_after * 0.8)   # en A, que cae otro 20 %
    assert r.equity_end == pytest.approx(coll_after - debt_after * 0.8)
    assert r.time_in["alcista"] == pytest.approx(10 / 30)


def test_cambio_parcial_solo_cobra_la_parte_que_cambia():
    stages = [2.0] * 10 + [3.0] * 10                       # E3 en la regla mixta → mitad a USDC
    r = M.simulate(_series([80_000.0] * 20, stages), _plan("semanal-mixto", coll_bps=30))
    assert r.switches == 1
    assert r.switch_cost_usd == pytest.approx(CAP * 0.5 * 0.003)
    assert r.time_in["mixto"] == pytest.approx(0.5)


def test_reglas_de_etapa_mantienen_o_pasan_a_mixto():
    df = _series([1.0] * 5, [2.0, 3.0, 1.0, 4.0, 3.0])
    assert M.targets(df, "semanal") == ["alcista", None, None, "bajista", None]
    assert M.targets(df, "semanal-mixto") == ["alcista", "mixto", "mixto", "bajista", "mixto"]
    assert M.targets(df, "siempre-neutral") == ["neutral"] * 5
    df["nivel"] = [None, "bajista", None, "alcista", None]
    assert M.targets(df, "nivel") == [None, "bajista", None, "alcista", None]


def test_sin_poder_pedir_el_activo_no_pasa_a_bajista():
    r = M.simulate(_series([80_000.0] * 20, [2.0] * 10 + [4.0] * 10, can_a=False), _plan("semanal"))
    assert r.switches == 0 and r.time_in["alcista"] == 1


def test_bajista_se_liquida_si_el_activo_se_dispara():
    r = M.simulate(_series([80_000.0] * 5 + [80_000.0 * 2.9] * 5), _plan("siempre-bajista"))
    debt = DEBT * 2.9                                      # HF = 11.500·0,78 / 10.005 ≈ 0,90 < 0,95
    assert r.liquidations == 1
    assert r.liquidation_loss_usd == pytest.approx(debt * 0.05)
    assert r.equity_end == pytest.approx(CAP - debt * 1.05)


def test_defender_devuelve_deuda_hasta_el_hf_inicial():
    prices = [80_000.0] * 5 + [36_000.0] * 5              # HF 2,6 → 1,17 < 1,3
    r = M.simulate(_series(prices), _plan("siempre-alcista", manage="defender"))
    hf0 = CAP * 0.78 / DEBT
    target = CAP * 0.45 * 0.78 / hf0
    assert r.defenses == 1 and r.liquidations == 0
    assert r.added_usd == pytest.approx(DEBT - target)
    assert r.hf_min == pytest.approx(hf0)


def test_niveles_de_abajo_y_de_arriba():
    base = {"close": 100.0, "last_ph": 110.0, "last_pl": 90.0, "range_top": 110.0, "range_bottom": 90.0,
            "break_up": float("nan"), "break_dn": float("nan")}
    assert M._band(pd.Series({**base, "stage": 2, "ma": 95.0})) == (95.0, 110.0)
    assert M._band(pd.Series({**base, "stage": 4, "ma": 105.0})) == (90.0, 105.0)
    assert M._band(pd.Series({**base, "stage": 1, "ma": 100.0})) == pytest.approx((90.0, 110.0 + BREAK_FRAC * 20))


def test_las_senales_solo_valen_desde_el_cierre():
    df = pd.DataFrame({"hour": pd.date_range("2026-01-05", periods=48, freq="h")})
    sig = pd.DataFrame([{"desde": pd.Timestamp("2026-01-05 00:00"), "st_w": 2.0, "nivel": "alcista"},
                        {"desde": pd.Timestamp("2026-01-06 00:00"), "st_w": 4.0, "nivel": "bajista"},
                        {"desde": pd.Timestamp("2026-01-05 00:00"), "st_d": 2.0}])
    out = M.attach_signals(df, sig)
    assert out.loc[out["hour"] < "2026-01-06", "st_w"].eq(2.0).all()
    assert out.loc[out["hour"] >= "2026-01-06", "nivel"].eq("bajista").all()
