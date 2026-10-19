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
    assert all(p.level in ("apta", "precaución") for p in liq.profiles)


def test_report_pool_links_go_to_uniswap():
    from etapas.liquidez import PoolQuote
    from etapas.pools import pools_for
    from etapas.report_liquidez import _quote_cells
    pool = next(p for p in pools_for("ETH")[0] if p.dex.startswith("Uniswap"))
    cell = _quote_cells(PoolQuote(pool, 1.0, 2.0, 1, 2), 1000)
    assert f'href="https://app.uniswap.org/explore/pools/{pool.chain}/{pool.address}"' in cell
    assert "geckoterminal" not in cell


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


def test_pool_column_separates_stage_from_profitability():
    """Paso 1 (color) y paso 2 (✓ y decisión): el ✓ solo sale si /pool propondría la combinación."""
    from types import SimpleNamespace as NS
    from etapas import oportunidad as op
    a = _pair()
    a.liquidity = analyze_liquidity(a, fetch=lambda pools: {})
    pool = NS(label="Uniswap v3 · Base · WETH/cbBTC 0,30 %")

    def opp(cycle, excluded, net_apr, net_safe=-5.0):
        o = op.Opportunity(cycle, cycle, 7.0, "apta", [], pool, None, op.Backtest(0.8, -0.01, 50))
        o.excluded, o.net_apr, o.fees_cycle, o.net_safe, o.aave_apy = excluded, net_apr, 10.0, net_safe, 0.0
        return o

    a.pool_report = NS(capital=5000, opportunities=[
        opp("1w", "resultado neto estimado negativo", -0.092),
        opp("1M", "", 0.05, net_safe=3.0), opp("1M", "", 0.03, net_safe=2.0)])
    out = matrix_html([a], [])
    assert "etapa apta:" in out and "favorable" not in out
    assert "decisión: ABRIR · mensual (+5.0%)" in out and "M✓" in out and "S✓" not in out
    card = pool_verdict_html(a)
    assert "✓ paso 2: abrir · +5.0% anual" in card
    assert ("paso 2: vigilar · resultado neto estimado negativo; para abrir necesitaría unas comisiones "
            "1,5 veces las de hoy (-9.2% anual)") in card
    a.pool_report = NS(capital=5000, opportunities=[opp("1w", "resultado neto estimado negativo", -0.092)])
    assert "decisión: vigilar · lo más cercano: semanal, faltan comisiones ×1,5" in matrix_html([a], [])
    a.pool_report = NS(capital=5000, opportunities=[])
    assert "decisión: sin pools" in matrix_html([a], [])
    a.pool_report = None
    assert "decisión:" not in matrix_html([a], [])


def test_open_position_hint_uses_the_two_steps():
    """posiciones.txt: dentro o fuera, la decisión de hoy dice si reajustar."""
    from types import SimpleNamespace as NS
    from etapas import oportunidad as op
    o = op.Opportunity("1w", "Semanal", 7.0, "apta", [], NS(label="Uniswap v3 · x"), None, op.Backtest(0.8, -0.01, 50))
    o.excluded, o.net_apr, o.fees_cycle, o.net_safe, o.aave_apy = "resultado neto estimado negativo", -0.05, 10.0, -5.0, 0.0
    rep = NS(capital=5000, opportunities=[o])
    assert "Hoy /pool: vigilar: no reajustes" in rutina.today_hint(rep, outside=True)
    assert "si sale del rango, no reajustes" in rutina.today_hint(rep, outside=False)
    o.excluded = ""
    assert "Hoy /pool: abrir (ciclo semanal, Uniswap v3 · x): reajusta" in rutina.today_hint(rep, outside=True)
