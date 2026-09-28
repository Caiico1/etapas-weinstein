"""Contexto de mercado (titulares, sentimiento, Reserva Federal) con fuentes simuladas."""
import datetime as dt
import json

import pandas as pd

from etapas import contexto
from etapas.analysis import AssetResult, TimeframeResult
from etapas.config import TIMEFRAMES
from etapas.contexto import MarketContext, fomc_notes, headlines_for, load_context, next_fomc
from etapas.report import context_html

RSS = """<rss><channel>
<item><title><![CDATA[Bitcoin falls to $83,000 as oil climbs]]></title><link>https://x/1</link>
<pubDate>Mon, 28 Sep 2026 10:15:00 +0000</pubDate></item>
<item><title>Solana ETFs draw record inflows</title><link>https://x/2</link>
<pubDate>Sun, 27 Sep 2026 06:21:00 +0000</pubDate></item>
<item><title>Ada Lovelace day: a sole developer story</title><link>https://x/3</link>
<pubDate>Sun, 27 Sep 2026 06:00:00 +0000</pubDate></item>
<item><title>Old bitcoin news</title><link>https://x/4</link>
<pubDate>Mon, 01 Sep 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""
FNG = json.dumps({"data": [{"value": str(v), "value_classification": c}
                           for v, c in [(74, "Greed")] + [(60, "Greed")] * 6 + [(40, "Fear")]]})
FOMC = """<h4>2026 FOMC Meetings</h4>
<div class="fomc-meeting__month col-xs-5"><strong>September</strong></div>
<div class="fomc-meeting__date col-xs-4">15-16*</div>
<div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div>
<div class="fomc-meeting__month col-xs-5"><strong>Oct/Nov</strong></div>
<div class="fomc-meeting__date col-xs-4">31-1 (notation vote)</div>"""


def _get(url):
    if "alternative" in url:
        return FNG
    if "federalreserve" in url:
        return FOMC
    if "decrypt" in url:
        raise OSError("sin conexión")
    return RSS


def _ctx():
    return load_context(pd.Timestamp("2026-09-28 06:00", tz="UTC"), get=_get)


def test_load_context_parses_all_sources_and_survives_failures():
    ctx = _ctx()
    assert "Decrypt" in ctx.sources_failed and "Cointelegraph" in ctx.sources_ok
    assert len(ctx.headlines) == 4                      # deduplicados entre fuentes
    assert ctx.fng == {"hoy": 74, "clase": "codicia", "hace7": 40, "clase7": "miedo"}
    assert (dt.date(2026, 10, 31), dt.date(2026, 11, 1)) in ctx.fomc   # reunión entre dos meses
    assert next_fomc(ctx) == (dt.date(2026, 10, 27), dt.date(2026, 10, 28))


def test_headline_filter_by_name_and_uppercase_ticker():
    ctx = _ctx()
    assert [h.link for h in headlines_for(ctx, "BTC")] == ["https://x/1"]     # la de septiembre es vieja
    assert [h.link for h in headlines_for(ctx, "SOL")] == ["https://x/2"]
    assert headlines_for(ctx, "ADA") == []              # "Ada Lovelace" no es Cardano ni "ADA"


def test_total_failure_gives_empty_context():
    def broken(url):
        raise OSError("caído")
    ctx = load_context(pd.Timestamp("2026-09-28", tz="UTC"), get=broken)
    assert ctx.headlines == [] and ctx.fng is None and ctx.fomc is None
    assert len(ctx.sources_failed) == len(contexto.FEEDS)


def _asset(candle_time_w="2026-10-19"):
    r = TimeframeResult("1w", TIMEFRAMES["1w"].label, "ok", stage=1, candle_time=candle_time_w)
    return AssetResult("BTC", {"1M": TimeframeResult("1M", "Mensual", "error", "-"), "1w": r,
                               "1d": TimeframeResult("1d", "Diario", "error", "-")})


def test_fomc_note_only_when_decision_falls_in_current_candle():
    ctx = _ctx()
    ctx.today = dt.date(2026, 10, 26)
    # última semana cerrada: 19/10 → la semana en curso (26/10–01/11) contiene la decisión del 28/10
    assert any("28/10" in n for n in fomc_notes(ctx, _asset("2026-10-19")))
    assert fomc_notes(ctx, _asset("2026-10-05")) == []


def test_context_section_in_report():
    page = context_html(_asset(), _ctx())
    assert "Contexto de mercado" in page and "no modifica la etapa" in page
    assert "Bitcoin falls" in page and "74 (codicia)" in page and "27/10" in page
    assert "Decrypt" in page                            # avisa de la fuente caída
    assert context_html(_asset(), None) == ""
