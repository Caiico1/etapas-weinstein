"""Test 2: casos límite. Historial insuficiente, serie plana, pico aislado y fallo de la API."""
import json

import ccxt
import numpy as np
import pandas as pd
import pytest

from etapas import data
from etapas.__main__ import main
from etapas.analysis import analyze_candles, analyze_symbol
from etapas.classifier import classify
from etapas.config import DISCLAIMER, TIMEFRAMES
from etapas.data import Candles, split_closed
from etapas.report import render_json, render_text
from synth import cycle_for, ohlcv_from_close, segments

D, W, M = TIMEFRAMES["1d"], TIMEFRAMES["1w"], TIMEFRAMES["1M"]


def _trend(n, start=100.0, drift=0.004, seed=0, freq="D"):
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))
    return ohlcv_from_close(close, rng, freq=freq)


# ------------------------------------------------------------ historial insuficiente

def test_insufficient_daily_history():
    df = _trend(40)
    res = analyze_candles(Candles(df, None, "test"), D)
    assert res.status == "insuficiente"
    assert "datos insuficientes" in res.message and "40" in res.message
    assert res.stage is None


def test_monthly_falls_back_to_alternative_ma():
    df = _trend(22, freq="MS")        # < 20 + 6 pero ≥ 12 + 6
    res = analyze_candles(Candles(df, None, "test"), M)
    assert res.status == "ok"
    assert res.ma_len == 12
    assert any("SMA12" in n for n in res.notes)


def test_monthly_too_short_even_for_alternative():
    df = _trend(10, freq="MS")
    res = analyze_candles(Candles(df, None, "test"), M)
    assert res.status == "insuficiente"


def test_short_history_warns():
    df = _trend(100)                  # suficiente para SMA50, pero < 3 × 50
    res = analyze_candles(Candles(df, None, "test"), D)
    assert res.status == "ok"
    assert any("Historial corto" in n for n in res.notes)


# ------------------------------------------------------------ serie completamente plana

def test_flat_series_does_not_break():
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                       "volume": 1000.0}, index=idx)
    res = analyze_candles(Candles(df, None, "test"), D)
    assert res.status == "ok"
    assert res.slope_label == "plana"
    assert res.stage in (1, 3)                   # rango, sin tendencia previa que decida
    assert res.confidence_label == "baja"
    assert res.structure == "sin pivotes"
    json.dumps(res.to_dict())                     # serializable, sin NaN sueltos


def test_zero_volume_series():
    df = _trend(300)
    df["volume"] = 0.0
    res = analyze_candles(Candles(df, None, "test"), D)
    assert res.status == "ok"
    assert res.volume_ratio is None


# ------------------------------------------------------------ pico aislado

def test_isolated_spike_in_uptrend_does_not_flip_stage():
    df = _trend(400, seed=3)
    i = 250
    df.iloc[i, df.columns.get_loc("high")] *= 1.6
    df.iloc[i, df.columns.get_loc("close")] *= 1.4
    stage = classify(df, D)["stage"]
    after = stage.iloc[i + 10:i + 60]
    assert (after == 2).mean() >= 0.9


def test_isolated_spike_on_last_bar_of_a_base():
    """Un pico en la última vela de una base no la convierte en etapa 2 con confianza alta."""
    df, labels = cycle_for(D, seed=7)
    _, a, b = [s for s in segments(labels) if s[0] == 1][0]
    base = df.iloc[: a + 200].copy()
    base.iloc[-1, base.columns.get_loc("high")] *= 1.5
    base.iloc[-1, base.columns.get_loc("close")] *= 1.3
    res = analyze_candles(Candles(base, None, "test"), D)
    assert res.status == "ok"
    assert not (res.stage == 2 and res.confidence_label == "alta")


# ------------------------------------------------------------ velas cerradas

def test_split_closed_drops_current_candle():
    idx = pd.date_range("2026-09-01", periods=5, freq="D", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    now = pd.Timestamp("2026-09-05 13:00", tz="UTC")
    closed, current = split_closed(df, "1d", now)
    assert len(closed) == 4 and current.name == idx[-1]
    monthly = df.iloc[:1].copy()
    monthly.index = pd.DatetimeIndex([pd.Timestamp("2026-09-01", tz="UTC")])
    closed, current = split_closed(monthly, "1M", now)
    assert closed.empty and current is not None


# ------------------------------------------------------------ fallo de la API

class _FailingExchange:
    def __init__(self, exc):
        self.exc = exc

    def load_markets(self):
        raise self.exc


class _FakeExchange:
    """Exchange de pega con velas generadas: solo para probar el fallback."""
    timeframes = {"1d": "1d", "1w": "1w"}          # sin '1M', como Kraken

    def __init__(self, pairs):
        self.markets = {p: {} for p in pairs}

    def load_markets(self):
        return self.markets

    def milliseconds(self):
        return int(pd.Timestamp("2026-09-25", tz="UTC").timestamp() * 1000)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        step = 86_400_000 if timeframe == "1d" else 7 * 86_400_000
        start = since - since % 86_400_000
        rows = []
        t = start
        while t <= self.milliseconds() and len(rows) < (limit or 1000):
            rows.append([t, 100.0, 101.0, 99.0, 100.0, 10.0])
            t += step
        return rows


@pytest.fixture
def exchanges(monkeypatch):
    """Sustituye la creación de exchanges y limpia la caché."""
    monkeypatch.setattr(data, "_exchanges", {})

    def install(mapping):
        monkeypatch.setattr(data, "_make_exchange", lambda name: mapping[name])
    return install


def test_api_failure_gives_clear_error(exchanges):
    exchanges({"binance": _FailingExchange(ccxt.NetworkError("timeout")),
               "binance_data": _FailingExchange(ccxt.ExchangeNotAvailable("451 restringido")),
               "kraken": _FailingExchange(ccxt.ExchangeNotAvailable("mantenimiento"))})
    asset = analyze_symbol("BTC")
    assert asset.error
    assert all(n in asset.error for n in ("binance ", "binance_data", "kraken"))
    assert all(r.status == "error" for r in asset.timeframes.values())
    text = render_text(asset)
    assert "ERROR" in text
    payload = json.loads(render_json([asset]))
    assert payload["activos"][0]["error"]
    assert payload["aviso"] == DISCLAIMER


def test_cli_exit_code_and_disclaimer_on_failure(exchanges, capsys):
    exchanges({"binance": _FailingExchange(ccxt.NetworkError("sin red")),
               "binance_data": _FailingExchange(ccxt.NetworkError("sin red")),
               "kraken": _FailingExchange(ccxt.NetworkError("sin red"))})
    code = main(["BTC"])
    out = capsys.readouterr().out
    assert code == 1
    assert "No se pudieron descargar datos" in out
    assert out.strip().endswith(DISCLAIMER)


def test_unknown_symbol(exchanges):
    exchanges({"binance": _FakeExchange([]), "binance_data": _FakeExchange([]),
               "kraken": _FakeExchange([])})
    asset = analyze_symbol("NOEXISTE")
    assert asset.error and "no existe" in asset.error


def test_binance_blocked_uses_data_api(exchanges):
    """api.binance.com bloqueada (451, como en EE. UU.) → se usa data-api.binance.vision."""
    exchanges({"binance": _FailingExchange(ccxt.ExchangeNotAvailable("451 restricted location")),
               "binance_data": _FakeExchange(["BTC/USDT"]),
               "kraken": _FailingExchange(ccxt.NetworkError("no debería llamarse"))})
    asset = analyze_symbol("BTC")
    assert asset.timeframes["1d"].source == "binance_data BTC/USDT"


def test_fallback_to_kraken_and_resample_monthly(exchanges):
    exchanges({"binance": _FailingExchange(ccxt.NetworkError("caído")),
               "binance_data": _FailingExchange(ccxt.NetworkError("caído")),
               "kraken": _FakeExchange(["BTC/USD"])})
    asset = analyze_symbol("BTC")
    daily, monthly = asset.timeframes["1d"], asset.timeframes["1M"]
    assert "kraken" in daily.source
    assert any("fallo en binance" in n for n in daily.notes)
    assert any("construidas a partir del diario" in n for n in monthly.notes)
    assert monthly.candles is None or monthly.candles.index[-1] < pd.Timestamp("2026-09-01", tz="UTC")
