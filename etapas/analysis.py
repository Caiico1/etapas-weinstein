"""Orquesta descarga + clasificación para un activo y sus tres marcos temporales."""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .classifier import STAGE_NAMES, classify, levels
from .config import ORDER, RANGE_QUANTILE, TIMEFRAMES, TimeframeConfig
from .data import Candles, DataError, fetch_candles, fetch_ondo_token, fetch_stock_candles
from .indicators import expected_range


@dataclass
class TimeframeResult:
    timeframe: str
    label: str
    status: str                       # "ok" | "insuficiente" | "error"
    message: str = ""
    notes: list[str] = field(default_factory=list)
    source: str = ""
    ma_len: int | None = None
    candle_time: str = ""
    stage: int | None = None
    stage_name: str = ""
    scores: dict = field(default_factory=dict)
    confidence: float | None = None
    confidence_label: str = ""
    transition: str = ""
    price: float | None = None
    ma: float | None = None
    slope: float | None = None
    slope_threshold: float | None = None
    slope_label: str = ""
    pct_above: float | None = None
    crosses: int | None = None
    structure: str = ""
    prior_change: float | None = None
    prior_since: str = ""
    range_top: float | None = None
    range_bottom: float | None = None
    volume_ratio: float | None = None
    breakout: str = ""
    confirm_level: float | None = None
    invalid_level: float | None = None
    est_low: float | None = None      # rango estimado de la vela en curso
    est_high: float | None = None
    est_period: str = ""
    last_price: float | None = None   # último precio conocido, incluida la vela en curso
    atr: float | None = None
    ma_run: int = 0                   # velas seguidas que la media sube (+) o baja (−)
    provisional: "TimeframeResult | None" = None
    history: pd.DataFrame | None = field(default=None, repr=False)   # para el HTML
    candles: pd.DataFrame | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("history", "candles", "provisional")}
        d["provisional"] = self.provisional.to_dict() if self.provisional else None
        return {k: _jsonable(v) for k, v in d.items()}


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else round(float(v), 6)
    if isinstance(v, np.integer):
        return int(v)
    return v


STOCK_PREFIXES = ("accion:", "acción:")


def parse_symbol(raw: str) -> tuple[str, str]:
    """'BTC' → ('cripto', 'BTC'); 'accion:NVDA' → ('accion', 'NVDA')."""
    s = raw.strip()
    if s.lower().startswith(STOCK_PREFIXES):
        return "accion", s.split(":", 1)[1].strip().upper()
    return "cripto", s.upper()


@dataclass
class AssetResult:
    symbol: str                        # ticker sin prefijo: BTC, NVDA, SAN.MC
    timeframes: dict[str, TimeframeResult]
    error: str = ""
    kind: str = "cripto"               # "cripto" | "accion"
    token: dict | None = None          # token de Ondo de la acción, si cotiza

    @property
    def key(self) -> str:
        """Identificador en la lista y en el historial."""
        return f"accion:{self.symbol}" if self.kind == "accion" else self.symbol

    @property
    def title(self) -> str:
        return f"{self.symbol} (acción)" if self.kind == "accion" else self.symbol

    @property
    def file_stem(self) -> str:
        return f"accion_{self.symbol}" if self.kind == "accion" else self.symbol

    @property
    def last_price(self) -> float | None:
        daily = self.timeframes.get("1d")
        return daily.last_price if daily else None

    def to_dict(self) -> dict:
        return {"simbolo": self.symbol, "tipo": self.kind, "error": self.error or None,
                "token_ondo": _jsonable(self.token) if self.token else None,
                "marcos": {k: v.to_dict() for k, v in self.timeframes.items()}}


def choose_ma(n_bars: int, cfg: TimeframeConfig) -> tuple[int | None, list[str]]:
    """Media a usar según el historial disponible, con aviso si se reduce."""
    if n_bars >= cfg.min_bars(cfg.ma):
        notes = []
        if n_bars < 3 * cfg.ma:
            notes.append(f"Historial corto: {n_bars} velas (< 3 × SMA{cfg.ma})")
        return cfg.ma, notes
    if cfg.ma_alt and n_bars >= cfg.min_bars(cfg.ma_alt):
        return cfg.ma_alt, [f"Solo {n_bars} velas: se usa SMA{cfg.ma_alt} en lugar de SMA{cfg.ma}"]
    return None, []


def _f(x):
    return None if x is None or pd.isna(x) else float(x)


def snapshot(hist: pd.DataFrame, cfg: TimeframeConfig, ma_len: int) -> TimeframeResult:
    """Resultado para la última vela de un histórico clasificado."""
    row = hist.iloc[-1]
    stage = int(row["stage"])
    if stage == 0:
        return TimeframeResult(cfg.key, cfg.label, "insuficiente", "datos insuficientes")
    confirm, invalid = levels(row)
    anchor = int(row["prior_anchor"])
    return TimeframeResult(
        timeframe=cfg.key, label=cfg.label, status="ok", ma_len=ma_len,
        candle_time=hist.index[-1].strftime("%Y-%m-%d"),
        stage=stage, stage_name=STAGE_NAMES[stage],
        scores={s: round(float(row[f"score_{s}"]), 1) for s in (1, 2, 3, 4)},
        confidence=_f(row["confidence"]), confidence_label=row["confidence_label"],
        transition=row["transition"],
        price=_f(row["close"]), ma=_f(row["ma"]),
        slope=_f(row["slope"]), slope_threshold=_f(row["flat_thr"]), slope_label=row["slope_label"],
        pct_above=_f(row["pct_above"]), crosses=int(row["crosses"]),
        structure=row["structure"],
        prior_change=_f(row["prior_chg"]),
        prior_since=hist.index[anchor].strftime("%Y-%m-%d") if anchor >= 0 else "",
        range_top=_f(row["range_top"]), range_bottom=_f(row["range_bottom"]),
        volume_ratio=_f(row["vol_ratio"]), breakout=row["breakout"],
        atr=_f(row["atr"]), ma_run=int(row["ma_run"]),
        confirm_level=_f(confirm), invalid_level=_f(invalid),
    )


def analyze_candles(candles: Candles, cfg: TimeframeConfig, provisional: bool = False) -> TimeframeResult:
    df = candles.df
    ma_len, notes = choose_ma(len(df), cfg)
    if ma_len is None:
        need = cfg.min_bars(cfg.ma_alt or cfg.ma)
        return TimeframeResult(cfg.key, cfg.label, "insuficiente",
                               f"datos insuficientes: {len(df)} velas cerradas, hacen falta {need}",
                               notes=candles.notes, source=candles.source, candles=df)
    hist = classify(df, cfg, ma_len)
    res = snapshot(hist, cfg, ma_len)
    res.notes = candles.notes + notes
    res.source = candles.source
    res.history, res.candles = hist, df
    res.last_price = _f(candles.current["close"]) if candles.current is not None else res.price
    if res.status == "ok":
        low, high = expected_range(df, hist["atr"], RANGE_QUANTILE, cfg.range_lookback)
        res.est_low, res.est_high, res.est_period = _f(low), _f(high), cfg.period_name
    if provisional and candles.current is not None:
        cur = pd.concat([df, candles.current.to_frame().T.astype(float)])
        res.provisional = snapshot(classify(cur, cfg, ma_len), cfg, ma_len)
        res.provisional.label = f"{cfg.label} (PROVISIONAL)"
    return res


def analyze_symbol(symbol: str, provisional: bool = False,
                   fetch: Callable | None = None) -> AssetResult:
    """Analiza un activo: 'BTC' (cripto, vía exchanges) o 'accion:NVDA' (bolsa, vía Yahoo)."""
    kind, ticker = parse_symbol(symbol)
    fetch = fetch or (fetch_stock_candles if kind == "accion" else fetch_candles)
    results = {}
    errors = []
    for key in ORDER:
        cfg = TIMEFRAMES[key]
        try:
            candles = fetch(ticker, cfg)
        except DataError as e:
            results[key] = TimeframeResult(key, cfg.label, "error", str(e))
            errors.append(str(e))
            continue
        results[key] = analyze_candles(candles, cfg, provisional)
    error = errors[0] if len(errors) == len(ORDER) else ""
    asset = AssetResult(ticker, results, error, kind)
    if kind == "accion" and not error:
        asset.token = fetch_ondo_token(ticker)
        if asset.token and asset.last_price:
            asset.token["diferencia"] = asset.token["precio"] / asset.last_price - 1
    return asset
