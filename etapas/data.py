"""Descarga de velas OHLCV con ccxt (Binance → Kraken) y separación de velas cerradas."""
from dataclasses import dataclass, field

import ccxt
import pandas as pd

from .config import SOURCES, TimeframeConfig

COLUMNS = ["open", "high", "low", "close", "volume"]
_APPROX_MS = {"1d": 86_400_000, "1w": 7 * 86_400_000, "1M": 31 * 86_400_000}
_RESAMPLE_RULE = {"1w": "W-MON", "1M": "MS"}


class DataError(Exception):
    """No se han podido obtener datos reales para un símbolo/timeframe."""


@dataclass
class Candles:
    df: pd.DataFrame                  # solo velas cerradas, índice UTC = apertura
    current: pd.Series | None         # vela en curso (no cerrada), si existe
    source: str                       # p. ej. "binance BTC/USDT"
    notes: list[str] = field(default_factory=list)


def candle_close_time(open_time: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    if timeframe == "1M":
        return open_time + pd.DateOffset(months=1)
    return open_time + pd.Timedelta(milliseconds=_APPROX_MS[timeframe])


def split_closed(df: pd.DataFrame, timeframe: str, now: pd.Timestamp):
    """Devuelve (velas cerradas, vela en curso o None)."""
    if df.empty:
        return df, None
    last = df.index[-1]
    if candle_close_time(last, timeframe) > now:
        return df.iloc[:-1], df.iloc[-1]
    return df, None


_exchanges: dict[str, ccxt.Exchange] = {}


def _make_exchange(name: str) -> ccxt.Exchange:
    params = {"enableRateLimit": True, "timeout": 20_000}
    if name == "binance_data":
        # API pública de solo datos de mercado de Binance: mismas velas, sin el bloqueo
        # geográfico de api.binance.com (p. ej. servidores en EE. UU. como los de GitHub).
        ex = ccxt.binance({**params, "options": {"fetchMarkets": ["spot"]}})
        for key, url in ex.urls["api"].items():
            if isinstance(url, str) and url.startswith("https://api.binance.com/api/v3"):
                ex.urls["api"][key] = url.replace("https://api.binance.com", "https://data-api.binance.vision")
        return ex
    return getattr(ccxt, name)(params)


def _get_exchange(name: str) -> ccxt.Exchange:
    if name not in _exchanges:
        ex = _make_exchange(name)
        ex.load_markets()
        _exchanges[name] = ex
    return _exchanges[name]


def _fetch_rows(ex: ccxt.Exchange, symbol: str, timeframe: str, bars: int) -> list:
    now_ms = ex.milliseconds()
    since = now_ms - bars * _APPROX_MS[timeframe]
    rows: list = []
    for _ in range(100):  # tope de páginas por seguridad
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        new = [r for r in batch if not rows or r[0] > rows[-1][0]]
        if not new:
            break
        rows.extend(new)
        since = rows[-1][0] + 1
        if rows[-1][0] + _APPROX_MS["1d"] > now_ms:  # ya llegamos a la vela actual
            break
    return rows


def _to_frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


def _resample(daily: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = _RESAMPLE_RULE[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = daily.resample(rule, label="left", closed="left").agg(agg).dropna()
    # La primera vela suele estar incompleta (el diario no empieza en su inicio): se descarta
    if len(out) and daily.index[0] > out.index[0]:
        out = out.iloc[1:]
    return out


def fetch_candles(symbol: str, cfg: TimeframeConfig, now: pd.Timestamp | None = None) -> Candles:
    """Descarga velas reales. Lanza DataError si todas las fuentes fallan."""
    now = now or pd.Timestamp.now(tz="UTC")
    errors = []
    for name, pattern in SOURCES:
        pair = pattern.format(sym=symbol.upper())
        try:
            ex = _get_exchange(name)
            if pair not in ex.markets:
                raise DataError(f"el par {pair} no existe")
            notes = []
            if cfg.key in ex.timeframes:
                rows = _fetch_rows(ex, pair, cfg.key, cfg.history + 2)
                df = _to_frame(rows) if rows else pd.DataFrame(columns=COLUMNS)
            else:
                days = cfg.history * (31 if cfg.key == "1M" else 7)
                rows = _fetch_rows(ex, pair, "1d", days)
                df = _resample(_to_frame(rows), cfg.key) if rows else pd.DataFrame(columns=COLUMNS)
                notes.append(f"{name} no ofrece velas {cfg.key}: construidas a partir del diario "
                             f"({len(rows)} velas diarias disponibles)")
            if df.empty:
                raise DataError("la fuente no devolvió velas")
            closed, current = split_closed(df, cfg.key, now)
            if name != SOURCES[0][0]:
                notes.append(f"Datos de {name} {pair} (fallo en {SOURCES[0][0]})")
            return Candles(closed, current, f"{name} {pair}", notes)
        except (ccxt.BaseError, DataError, OSError) as e:
            errors.append(f"{name} {pair}: {type(e).__name__}: {str(e)[:160]}")
    raise DataError("No se pudieron descargar datos. " + " | ".join(errors))


# ---------------------------------------------------------------- acciones (Yahoo Finance)

_YF_INTERVAL = {"1d": "1d", "1w": "1wk", "1M": "1mo"}


def fetch_stock_candles(ticker: str, cfg: TimeframeConfig, now: pd.Timestamp | None = None) -> Candles:
    """Velas de una acción o ETF desde Yahoo Finance (ticker de Yahoo: NVDA, SPY, SAN.MC...).

    Los precios se ajustan por dividendos y splits: es la rentabilidad total, la misma que
    replican los tokens de Ondo (NVDAon...). Lanza DataError si no hay datos.
    """
    import yfinance as yf

    now = now or pd.Timestamp.now(tz="UTC")
    try:
        raw = yf.Ticker(ticker).history(period="max", interval=_YF_INTERVAL[cfg.key], auto_adjust=True)
    except Exception as e:  # yfinance lanza excepciones de varios tipos
        raise DataError(f"Yahoo Finance {ticker}: {type(e).__name__}: {str(e)[:160]}") from e
    if raw is None or raw.empty or not isinstance(raw.index, pd.DatetimeIndex):
        raise DataError(f"Yahoo Finance no tiene datos de {ticker}. Revisa el ticker "
                        f"(ejemplos: NVDA, SPY, SAN.MC para bolsa española)")
    df = raw.rename(columns=str.lower)[COLUMNS].astype(float).dropna(subset=["open", "high", "low", "close"])
    # Fecha local del mercado (una sesión de Madrid del día 24 es la vela del 24, no del 23 en UTC)
    df.index = pd.DatetimeIndex(df.index.tz_localize(None).normalize()).tz_localize("UTC")
    df.index.name = "time"
    df = df[~df.index.duplicated(keep="last")].tail(cfg.history + 1)
    closed, current = split_closed(df, cfg.key, now)
    return Candles(closed, current, f"Yahoo Finance {ticker} (ajustado por dividendos y splits)")


# ---------------------------------------------------------------- tokens de Ondo (NVDAon...)

ONDO_VENUES = [("mexc", "{t}ON/USDT"), ("bingx", "{t}ON/USDT")]


def fetch_ondo_token(ticker: str) -> dict | None:
    """Precio actual del token de Ondo de una acción de EE. UU. (p. ej. NVDA → NVDAon), si cotiza
    en algún exchange de ONDO_VENUES. Devuelve None si no existe o no se puede consultar."""
    if not ticker.isalnum():          # Ondo solo tokeniza acciones de EE. UU. (sin sufijo de mercado)
        return None
    for name, pattern in ONDO_VENUES:
        pair = pattern.format(t=ticker.upper())
        try:
            ex = _get_exchange(name)
            if pair not in ex.markets:
                continue
            price = ex.fetch_ticker(pair).get("last")
            if price:
                return {"token": f"{ticker.upper()}on", "exchange": name, "par": pair, "precio": float(price)}
        except (ccxt.BaseError, OSError):
            continue
    return None
