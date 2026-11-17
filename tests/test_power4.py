"""Etapa Power4 (reglas de MetaPro): entradas, permanencia y salidas sobre series controladas."""
import numpy as np
import pandas as pd

from etapas import power4
from etapas.rutina import diff


def _df(close) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1.0}, index=idx)


def test_needs_sma20():
    assert power4.latest(_df(np.linspace(100, 120, 19)), 3) == (None, "")


def test_short_history_is_incipient():
    """Con menos de 40 velas (JUP en mensual) MetaPro da «Posible E4 (incipiente)» por la SMA20."""
    stage, why = power4.latest(_df(np.linspace(200, 100, 35)), 3)
    assert stage == 4 and "Posible E4 (incipiente)" in why
    assert power4.latest(_df(np.linspace(100, 200, 35)), 3)[0] == 2


def test_live_candle_can_change_stage():
    """Mensual: la vela en curso cuenta (BTC pasó de 1 a 4 en nov-2026 sin cerrar vela)."""
    df = _df(np.concatenate([np.linspace(100, 200, 50), [190, 185, 180, 185, 190],
                             np.linspace(195, 230, 15)]))
    closed, _ = power4.latest(df, 3)
    cur = df.iloc[-1].copy()
    cur[["open", "high", "low", "close"]] = [60.0, 61.0, 59.0, 60.0]
    live, _ = power4.latest(df, 3, cur)
    assert closed == 2 and live == 3


def test_uptrend_is_advance_and_downtrend_decline():
    assert power4.latest(_df(np.linspace(100, 200, 80)), 3)[0] == 2
    assert power4.latest(_df(np.linspace(200, 100, 80)), 3)[0] == 4


def test_decline_holds_until_three_closes_above_sma20():
    down = np.linspace(200, 100, 80)
    # Rebote corto: el cierre supera la SMA20 (~112) pero no la SMA40 (~125), ni 3 veces seguidas
    one = np.concatenate([down, [120, 105]])
    st = power4.classify(_df(one), 3)
    assert st["p4"].iloc[-2] == 4 and st["above"].iloc[-2] == 1
    three = np.concatenate([down, [120, 121, 122]])
    assert power4.classify(_df(three), 3)["p4"].iloc[-1] == 1


def test_decline_ends_closing_above_both_averages():
    """JUP 6H (17-nov-2026): cierre sobre la SMA20 y la SMA40 con la SMA20 sin bajar ⇒ 1."""
    st = power4.classify(_df(np.concatenate([np.linspace(200, 100, 80), [140]])), 3)
    assert st["p4"].iloc[-1] == 1 and st["how"].iloc[-1] == "medias"


def test_accumulation_needs_entry_low_broken_to_decline():
    """SOL mensual (17-nov-2026): en 1, las condiciones de 4 no bastan; hay que perder el mínimo
    de la vela que abrió la etapa 1."""
    def run(last):
        df = _df(np.concatenate([np.linspace(200, 100, 80), [140, last]]))
        df.iloc[-2, df.columns.get_loc("low")] = 90.0   # la vela que abre la etapa 1 deja mecha hasta 90
        return power4.classify(df, 3)["p4"].iloc[-2:].tolist()

    assert run(100) == [1, 1]     # bajo las dos medias y la SMA20 bajando, pero sobre 90
    assert run(85) == [1, 4]


def test_advance_ends_in_distribution_not_decline():
    up = np.linspace(100, 200, 80)
    st = power4.classify(_df(np.concatenate([up, [180, 179, 178]])), 3)["p4"]
    assert st.iloc[-1] == 3


def test_reason_mentions_rule():
    stage, why = power4.latest(_df(np.linspace(200, 100, 80)), 3)
    assert stage == 4 and "último máximo relevante" in why and "SMA20" in why


def test_power4_change_is_reported():
    prev = {"ETH": {"1d": {"etapa": 3, "transicion": "", "power4": 4}}}
    curr = {"ETH": {"1d": {"etapa": 3, "transicion": "", "power4": 1}}}
    assert [c["texto"] for c in diff(prev, curr)] == ["Power4 4 → 1"]
    # Instantáneas anteriores sin Power4: no es un cambio
    prev["ETH"]["1d"].pop("power4")
    assert diff(prev, curr) == []


def test_divergence_only_against_the_trend():
    assert power4.divergence(2, 2) is None and power4.divergence(4, 4) is None
    assert power4.divergence(1, 4) is None and power4.divergence(3, 2) is None   # sin tendencia
    assert power4.divergence(2, 3) and power4.divergence(4, 1)
    text = power4.divergence(2, 4, 80154, 79800)
    assert "Weinstein en 2 (Alcista) y Power4 en 4 (Declive)" in text
    assert "cierre bajo 79,800 (a 0.4% del cierre)" in text
    assert "sobre" in power4.divergence(4, 1, 100, 110)
