"""Contexto de mercado: titulares, sentimiento cripto y reuniones de la Reserva Federal.

Es información complementaria: NO modifica la etapa ni los niveles, que siguen saliendo solo de
reglas técnicas reproducibles. Todas las fuentes son públicas y sin clave; si alguna falla, esa
parte queda como "no disponible" y el resto del informe se genera igual.
"""
import datetime as dt
import email.utils
import html
import json
import re
import urllib.request
from dataclasses import dataclass, field

import pandas as pd

from .data import candle_close_time

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
# Un valor por día (el primero es el de hoy). 61 días para poder mirar hasta hace 2 meses.
FNG_URL = "https://api.alternative.me/fng/?limit=61"
# Comparaciones del índice de miedo y codicia: (días atrás, clave)
FNG_LAGS = [(7, "hace7"), (30, "hace30"), (60, "hace60")]
FNG_LAG_LABELS = {"hace7": "hace 7 días", "hace30": "hace 1 mes", "hace60": "hace 2 meses"}
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# Nombres que identifican cada criptomoneda en un titular (sin distinguir mayúsculas). El ticker se
# busca además tal cual, en mayúsculas, para no confundir "SOL" o "ADA" con palabras corrientes.
NAMES = {"BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "ADA": ["cardano"],
         "XRP": ["ripple"], "DOGE": ["dogecoin"], "BNB": ["bnb chain"], "AVAX": ["avalanche"]}
HEADLINE_DAYS = 7
HEADLINE_LIMIT = 5

_FNG_ES = {"Extreme Fear": "miedo extremo", "Fear": "miedo", "Neutral": "neutral",
           "Greed": "codicia", "Extreme Greed": "codicia extrema"}
_MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december"]


@dataclass
class Headline:
    when: dt.datetime
    title: str
    link: str
    source: str


@dataclass
class MarketContext:
    headlines: list[Headline] = field(default_factory=list)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    fng: dict | None = None                     # {"hoy", "clase", "hace7", "clase7", "hace30", ...}
    fomc: list[tuple[dt.date, dt.date]] | None = None
    today: dt.date = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).date())


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (etapas-weinstein)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def _text(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_rss(xml: str, source: str) -> list[Headline]:
    out = []
    for item in re.findall(r"<item\b.*?>(.*?)</item>", xml, re.S):
        title = re.search(r"<title\b[^>]*>(.*?)</title>", item, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S) or re.search(r"<guid\b[^>]*>(.*?)</guid>", item, re.S)
        if not (title and date):
            continue
        try:
            when = email.utils.parsedate_to_datetime(date.group(1).strip())
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        out.append(Headline(when, _text(title.group(1)), _text(link.group(1)) if link else "", source))
    return out


def parse_fng(payload: str) -> dict | None:
    """Valor de hoy y de hace 7, 30 y 60 días. Cada valor se busca por su fecha (no por su posición
    en la lista, por si a la API le falta algún día); si esa fecha no está, se usa el día anterior
    más cercano, hasta 2 días antes. Si no hay ninguno, esa comparación queda en None."""
    data = json.loads(payload).get("data") or []
    if not data:
        return None
    clase = lambda d: _FNG_ES.get(d["value_classification"], d["value_classification"])
    today = data[0]
    out = {"hoy": int(today["value"]), "clase": clase(today)}
    if all("timestamp" in d for d in data):
        by_day = {dt.datetime.fromtimestamp(int(d["timestamp"]), dt.timezone.utc).date(): d for d in data}
        day0 = max(by_day)
        for days, key in FNG_LAGS:
            target = day0 - dt.timedelta(days=days)
            d = next((by_day[target - dt.timedelta(days=k)] for k in range(3)
                      if target - dt.timedelta(days=k) in by_day), None)
            out[key], out["clase" + key[4:]] = (int(d["value"]), clase(d)) if d else (None, None)
    else:                                   # sin fechas: posición en la lista (un valor por día)
        for days, key in FNG_LAGS:
            d = data[days] if len(data) > days else None
            out[key], out["clase" + key[4:]] = (int(d["value"]), clase(d)) if d else (None, None)
    return out


def fng_history(fng: dict) -> list[tuple[str, int, str]]:
    """[(«hace 7 días», valor, clase), ...] con las comparaciones disponibles."""
    return [(FNG_LAG_LABELS[key], fng[key], fng["clase" + key[4:]])
            for _, key in FNG_LAGS if fng.get(key) is not None]


def parse_fomc(page: str) -> list[tuple[dt.date, dt.date]]:
    """Reuniones del FOMC (inicio, fin) a partir de la página oficial de la Reserva Federal."""
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4}) FOMC Meetings", page)]
    meetings = []
    for n, (pos, year) in enumerate(heads):
        seg = page[pos: heads[n + 1][0] if n + 1 < len(heads) else len(page)]
        months = [_text(m) for m in re.findall(r"fomc-meeting__month[^>]*>(.*?)</div>", seg, re.S)]
        days = [_text(d) for d in re.findall(r"fomc-meeting__date[^>]*>(.*?)</div>", seg, re.S)]
        for month_txt, day_txt in zip(months, days):
            ms = [next((i for i, name in enumerate(_MONTHS_EN, 1) if name.startswith(p.strip().lower()[:3])), None)
                  for p in month_txt.split("/") if p.strip()]
            nums = [int(d) for d in re.findall(r"\d+", day_txt.split(" ")[0])]
            if not ms or None in ms or not nums:
                continue
            try:
                meetings.append((dt.date(year, ms[0], nums[0]), dt.date(year, ms[-1], nums[-1])))
            except ValueError:
                continue
    return sorted(set(meetings))


def load_context(now: pd.Timestamp | None = None, get=_get) -> MarketContext:
    """Descarga todas las fuentes. Nunca lanza excepciones: lo que falla queda anotado."""
    now = now or pd.Timestamp.now(tz="UTC")
    ctx = MarketContext(today=now.date())
    seen = set()
    for name, url in FEEDS.items():
        try:
            items = parse_rss(get(url), name)
        except Exception:                        # red, formato… cualquier fallo de una fuente
            ctx.sources_failed.append(name)
            continue
        ctx.sources_ok.append(name)
        for h in items:
            key = re.sub(r"\W+", "", h.title.lower())
            if key not in seen:
                seen.add(key)
                ctx.headlines.append(h)
    ctx.headlines.sort(key=lambda h: h.when, reverse=True)
    try:
        ctx.fng = parse_fng(get(FNG_URL))
    except Exception:
        ctx.fng = None
    try:
        ctx.fomc = parse_fomc(get(FOMC_URL)) or None
    except Exception:
        ctx.fomc = None
    return ctx


def headlines_for(ctx: MarketContext, ticker: str, kind: str = "cripto",
                  days: int = HEADLINE_DAYS, limit: int = HEADLINE_LIMIT) -> list[Headline]:
    """Titulares recientes que mencionan el valor (por nombre o por ticker en mayúsculas)."""
    patterns = [re.compile(rf"\b{re.escape(n)}\b", re.I) for n in NAMES.get(ticker.upper(), [])] \
        if kind == "cripto" else []
    patterns.append(re.compile(rf"\b{re.escape(ticker.upper())}\b"))
    since = dt.datetime.combine(ctx.today, dt.time(), tzinfo=dt.timezone.utc) - dt.timedelta(days=days - 1)
    found = [h for h in ctx.headlines if h.when >= since and any(p.search(h.title) for p in patterns)]
    return found[:limit]


def next_fomc(ctx: MarketContext) -> tuple[dt.date, dt.date] | None:
    if not ctx.fomc:
        return None
    return next(((a, b) for a, b in ctx.fomc if b >= ctx.today), None)


_CANDLE_NAME = {"1d": "La vela diaria de hoy", "1w": "La vela semanal en curso", "1M": "La vela mensual en curso"}


def fomc_notes(ctx: MarketContext, asset) -> list[str]:
    """Avisos cuando la decisión de una reunión de la Fed cae dentro de una vela en curso."""
    if not ctx.fomc or asset.error:
        return []
    notes = []
    for k in ("1M", "1w", "1d"):
        r = asset.timeframes.get(k)
        if r is None or r.status != "ok" or not r.candle_time:
            continue
        open_ = candle_close_time(pd.Timestamp(r.candle_time, tz="UTC"), k)
        close = candle_close_time(open_, k)
        for a, b in ctx.fomc:
            decision = pd.Timestamp(b, tz="UTC")
            if b >= ctx.today and open_ <= decision < close:
                notes.append(f"{_CANDLE_NAME[k]} incluye la decisión de la Reserva Federal del "
                             f"{b:%d/%m}: estas reuniones suelen aumentar la volatilidad, así que el "
                             f"precio puede salirse del rango típico.")
    return notes
