"""Pares en la lista de seguimiento, matriz de etapas, barras de rango y veredicto de pool (sin red)."""
from etapas import rutina
from etapas.analysis import AssetResult, analyze_candles, analyze_symbol, parse_symbol
from etapas.config import TIMEFRAMES
from etapas.data import Candles
from etapas.liquidez import analyze_liquidity
from etapas.report_panel import matrix_html, pool_verdict_html, range_bar, range_bars_html
from synth import cycle_for


def _fetch(ticker, cfg):
    df, _ = cycle_for(cfg, seed=1)
    return Candles(df, None, "sintético")


def _pair(symbol="ETH/BTC"):
    return analyze_symbol(f"par:{symbol}", fetch=_fetch)


def test_parse_and_load_pairs(tmp_path):
    assert parse_symbol("par: eth / btc") == ("par", "ETH/BTC")
    f = tmp_path / "watchlist.txt"
    f.write_text("BTC\npar:ETH/BTC\npar:eth/btc\npar:ETHBTC\n", encoding="utf-8")
    symbols, warnings = rutina.load_watchlist(f)
    assert symbols == ["BTC", "par:ETH/BTC"]
    assert any("repetido" in w for w in warnings) and any("ETHBTC" in w for w in warnings)


def test_pair_asset_identity_and_liquidity():
    a = _pair()
    assert (a.kind, a.key, a.file_stem, a.base, a.quote) == ("par", "par:ETH/BTC", "par_ETH-BTC", "ETH", "BTC")
    assert not a.error and a.last_price
    liq = analyze_liquidity(a, fetch=lambda pools: {})
    assert liq.quote == "BTC" and liq.pools == []
    assert "etapas.oportunidad ETH/BTC" in liq.note
    assert all(p.level in ("favorable", "precaución", "desfavorable") for p in liq.profiles)


def test_agent_prompt_file_for_pair(tmp_path):
    from etapas.agentes import write_prompt
    assert write_prompt({"activo": "ETH/BTC"}, tmp_path).name == "ETH-BTC.md"


def test_matrix_marks_changes_and_links_pairs():
    a = _pair()
    a.liquidity = analyze_liquidity(a, fetch=lambda pools: {})
    bad = AssetResult("MALO", {}, "sin datos de ningún exchange")
    out = matrix_html([a, bad], [{"simbolo": "par:ETH/BTC", "marco": "Semanal", "texto": "etapa 1 → 2"}])
    assert 'href="etapas_par_ETH-BTC.html"' in out
    assert out.count('class="mnew"') == 1
    assert "sin datos de ningún exchange" in out
    assert 'data-f="e' in out and "nuevo" in out


def test_range_bar_places_price_and_levels():
    cfg = TIMEFRAMES["1d"]
    df, _ = cycle_for(cfg, seed=1)
    r = analyze_candles(Candles(df, None, "sintético"), cfg)
    html = range_bar(r, r.range_top * 1.5)
    assert "por encima de la resistencia" in html and "Soporte" in html and "Resistencia" in html
    inside = range_bar(r, (r.range_top + r.range_bottom) / 2)
    assert "al 50 % del rango" in inside.replace("50%", "50 %")


def test_pool_verdict_card_shows_profiles_and_command():
    a = _pair()
    a.liquidity = analyze_liquidity(a, fetch=lambda pools: {})
    card = pool_verdict_html(a, "https://github.com/x/y/actions/workflows/pool.yml")
    assert "¿Pool ahora? ETH/BTC" in card
    assert all(name in card for name in ("Diaria", "Semanal", "Mensual"))
    assert "python -m etapas.oportunidad ETH/BTC" in card and "desde GitHub" in card
    assert range_bars_html(a).count('class="bar-card"') == 3
    assert pool_verdict_html(AssetResult("NVDA", {}, kind="accion")) == ""
