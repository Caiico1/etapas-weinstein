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
    assert ctx.fng == {"hoy": 74, "clase": "codicia", "hace7": 40, "clase7": "miedo",
                       "hace30": None, "clase30": None, "hace60": None, "clase60": None}
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


def test_fng_history_by_date_with_missing_day():
    """Hoy, hace 7, 30 y 60 días por fecha; si falta un día se usa el anterior más cercano."""
    day0 = dt.datetime(2026, 9, 29, tzinfo=dt.timezone.utc)
    values = {0: (73, "Greed"), 7: (78, "Extreme Greed"), 31: (69, "Greed"), 60: (25, "Extreme Fear")}
    data = [{"value": str(values.get(k, (50, "Neutral"))[0]),
             "value_classification": values.get(k, (50, "Neutral"))[1],
             "timestamp": str(int((day0 - dt.timedelta(days=k)).timestamp()))}
            for k in range(61) if k != 30]                 # falta el día de hace 30
    fng = contexto.parse_fng(json.dumps({"data": data}))
    assert (fng["hoy"], fng["hace7"], fng["hace30"], fng["hace60"]) == (73, 78, 69, 25)
    assert fng["clase7"] == "codicia extrema" and fng["clase60"] == "miedo extremo"
    assert contexto.fng_history(fng) == [("hace 7 días", 78, "codicia extrema"), ("hace 1 mes", 69, "codicia"),
                                         ("hace 2 meses", 25, "miedo extremo")]
    short = contexto.parse_fng(json.dumps({"data": data[:8]}))   # solo una semana de datos
    assert short["hace7"] == 78 and short["hace30"] is None and contexto.fng_history(short)[-1][0] == "hace 7 días"


def test_index_strip_shows_fng_history():
    from etapas.report import _context_strip
    ctx = contexto.MarketContext(fng={"hoy": 73, "clase": "codicia", "hace7": 78, "clase7": "codicia extrema",
                                      "hace30": 69, "clase30": "codicia", "hace60": 25, "clase60": "miedo extremo"})
    strip = _context_strip(ctx)
    assert "<b>73 · codicia</b>" in strip
    assert "(hace 7 días: 78)</span>" in strip and "(hace 1 mes: 69)</span>" in strip
    assert "(hace 2 meses: 25)</span>" in strip
