"""Orquesta descarga + clasificación para un activo y sus tres marcos temporales."""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .classifier import STAGE_NAMES, classify, levels
from .config import ORDER, TIMEFRAMES, TimeframeConfig
from .data import Candles, DataError, fetch_candles


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


@dataclass
class AssetResult:
    symbol: str
    timeframes: dict[str, TimeframeResult]
    error: str = ""

    def to_dict(self) -> dict:
        return {"simbolo": self.symbol, "error": self.error or None,
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
    if provisional and candles.current is not None:
        cur = pd.concat([df, candles.current.to_frame().T.astype(float)])
        res.provisional = snapshot(classify(cur, cfg, ma_len), cfg, ma_len)
        res.provisional.label = f"{cfg.label} (PROVISIONAL)"
    return res


def analyze_symbol(symbol: str, provisional: bool = False,
                   fetch: Callable = fetch_candles) -> AssetResult:
    results = {}
    errors = []
    for key in ORDER:
        cfg = TIMEFRAMES[key]
        try:
            candles = fetch(symbol, cfg)
        except DataError as e:
            results[key] = TimeframeResult(key, cfg.label, "error", str(e))
            errors.append(str(e))
            continue
        results[key] = analyze_candles(candles, cfg, provisional)
    error = errors[0] if len(errors) == len(ORDER) else ""
    return AssetResult(symbol.upper(), results, error)
