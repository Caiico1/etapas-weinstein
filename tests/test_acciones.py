"""Acciones (Yahoo Finance) y tokens de Ondo, sin red."""
import sys
import types

import pandas as pd
import pytest

from etapas import analysis, data, rutina
from etapas.analysis import parse_symbol
from etapas.config import TIMEFRAMES
from etapas.data import DataError, fetch_stock_candles
from etapas.report import render_text, write_html, write_index
from synth import cycle_for


def test_parse_symbol():
    assert parse_symbol("btc") == ("cripto", "BTC")
    assert parse_symbol("accion:nvda") == ("accion", "NVDA")
    assert parse_symbol("Acción: san.mc ") == ("accion", "SAN.MC")


def test_watchlist_accepts_stocks(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text("BTC\naccion:NVDA\nacción:san.mc\naccion:NVDA\nNVDA\n", encoding="utf-8")
    symbols, warnings = rutina.load_watchlist(f)
    assert symbols == ["BTC", "accion:NVDA", "accion:SAN.MC", "NVDA"]   # NVDA cripto ≠ acción
    assert len(warnings) == 1


def _fake_yfinance(monkeypatch, frames):
    """Sustituye yfinance por un módulo de pega que devuelve `frames[interval]`."""
    class Ticker:
        def __init__(self, t):
            self.t = t

        def history(self, period, interval, auto_adjust):
            return frames.get((self.t, interval), pd.DataFrame())
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))


def _yahoo_frame(df, tz):
    out = df.rename(columns=str.title).copy()
    out.index = out.index.tz_convert(None).tz_localize(tz)
    return out


def test_stock_candles_use_local_market_date(monkeypatch):
    df, _ = cycle_for(TIMEFRAMES["1d"], seed=1)
    _fake_yfinance(monkeypatch, {("SAN.MC", "1d"): _yahoo_frame(df, "Europe/Madrid")})
    now = df.index[-1] + pd.Timedelta(hours=12)
    c = fetch_stock_candles("SAN.MC", TIMEFRAMES["1d"], now=now)
    assert c.df.index[-1] == df.index[-2]          # la última vela (en curso) se separa
    assert c.current is not None
    assert "ajustado por dividendos" in c.source


def test_stock_unknown_ticker(monkeypatch):
    _fake_yfinance(monkeypatch, {})
    with pytest.raises(DataError, match="no tiene datos"):
        fetch_stock_candles("NOEXISTE", TIMEFRAMES["1d"])


class _Venue:
    def __init__(self, prices):
        self.markets = {p: {} for p in prices}
        self.prices = prices

    def load_markets(self):
        return self.markets

    def fetch_ticker(self, pair):
        return {"last": self.prices[pair]}


def test_ondo_token_lookup(monkeypatch):
    monkeypatch.setattr(data, "_exchanges", {})
    venues = {"mexc": _Venue({"NVDAON/USDT": 101.0}), "bingx": _Venue({})}
    monkeypatch.setattr(data, "_make_exchange", lambda name: venues[name])
    assert data.fetch_ondo_token("NVDA")["token"] == "NVDAon"
    assert data.fetch_ondo_token("AAPL") is None           # no cotiza
    assert data.fetch_ondo_token("SAN.MC") is None         # Ondo no tokeniza bolsa europea


def test_stock_asset_end_to_end(monkeypatch, tmp_path):
    def fake_fetch(ticker, cfg):
        df, _ = cycle_for(cfg, seed=2)
        return data.Candles(df, None, "Yahoo Finance (test)")
    monkeypatch.setattr(analysis, "fetch_stock_candles", fake_fetch)
    monkeypatch.setattr(analysis, "fetch_ondo_token",
                        lambda t: {"token": f"{t}on", "exchange": "mexc", "par": f"{t}ON/USDT", "precio": 100.0})
    asset = analysis.analyze_symbol("accion:NVDA")
    assert asset.kind == "accion" and asset.key == "accion:NVDA" and asset.title == "NVDA (acción)"
    assert asset.token["diferencia"] == pytest.approx(100.0 / asset.last_price - 1)
    assert "Token Ondo NVDAon" in render_text(asset)
    path = write_html(asset, tmp_path, index_link=True)
    assert path.name == "etapas_accion_NVDA.html"
    index = write_index([asset], [], None, [], tmp_path, pd.Timestamp("2026-09-25", tz="UTC"))
    html = index.read_text(encoding="utf-8")
    assert 'href="etapas_accion_NVDA.html"' in html and "NVDAon" in html
    assert rutina.snapshot([asset]).keys() == {"accion:NVDA"}
