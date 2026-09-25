"""Indicadores: SMA, ATR, pendiente, posición respecto a la media, pivotes y estructura."""
import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR de Wilder."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def slope(ma: pd.Series, n: int) -> pd.Series:
    """Pendiente relativa: (MA_t − MA_{t−n}) / MA_{t−n}."""
    return ma / ma.shift(n) - 1


def pct_above(close: pd.Series, ma: pd.Series, n: int) -> pd.Series:
    """Fracción de las últimas n velas que cerraron por encima de la media."""
    above = (close > ma).astype(float).where(ma.notna())
    return above.rolling(n, min_periods=n).mean()


def crosses(close: pd.Series, ma: pd.Series, n: int) -> pd.Series:
    """Número de veces que el cierre cambió de lado de la media en las últimas n velas."""
    side = np.sign(close - ma)
    changed = (side != side.shift(1)) & side.notna() & side.shift(1).notna() & (side != 0)
    return changed.astype(float).rolling(n, min_periods=n).sum()


def pivots(df: pd.DataFrame, k: int) -> tuple[pd.Series, pd.Series]:
    """Swing highs/lows: extremo de la ventana ±k y estrictamente mayor/menor que las k velas
    anteriores (así una meseta no genera un pivote en cada vela). Se conocen k velas después."""
    w = 2 * k + 1
    high, low = df["high"], df["low"]
    is_ph = (high == high.rolling(w, center=True).max()) & (high > high.shift(1).rolling(k).max())
    is_pl = (low == low.rolling(w, center=True).min()) & (low < low.shift(1).rolling(k).min())
    return is_ph.fillna(False), is_pl.fillna(False)


def structure(df: pd.DataFrame, k: int) -> pd.DataFrame:
    """Para cada vela, compara los dos últimos pivotes confirmados hasta ese momento.

    Un cierre por encima del último máximo pivote cuenta como máximo creciente, y uno por debajo
    del último mínimo pivote como mínimo decreciente, sin esperar a que se forme el pivote.
    """
    is_ph, is_pl = pivots(df, k)
    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ph_flags, pl_flags = is_ph.to_numpy(), is_pl.to_numpy()
    highs: list[float] = []
    lows: list[float] = []
    rows = []
    for t in range(len(df)):
        c = t - k  # el pivote en c queda confirmado en t
        if c >= 0:
            if ph_flags[c]:
                highs.append(high[c])
            if pl_flags[c]:
                lows.append(low[c])
        hh = hl = None
        if len(highs) >= 2:
            hh = highs[-1] > highs[-2]
        if len(lows) >= 2:
            hl = lows[-1] > lows[-2]
        if highs and close[t] > highs[-1]:
            hh = True
        if lows and close[t] < lows[-1]:
            hl = False
        rows.append((hh, hl,
                     highs[-1] if highs else np.nan,
                     lows[-1] if lows else np.nan))
    out = pd.DataFrame(rows, index=df.index, columns=["hh", "hl", "last_ph", "last_pl"])
    out["structure"] = [structure_label(h, l) for h, l in zip(out["hh"], out["hl"])]
    return out


def structure_label(hh, hl) -> str:
    if hh is None and hl is None:
        return "sin pivotes"
    h = "?" if hh is None else ("HH" if hh else "LH")
    l = "?" if hl is None else ("HL" if hl else "LL")
    return f"{h}/{l}"
