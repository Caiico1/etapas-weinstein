"""Etapa según el sistema Power4 de MetaPro Academy, para compararla con la de Weinstein.

Reglas (de cómo MetaPro explica sus etapas), sobre velas cerradas:
  - Entra en 2 (Avance) con el cierre sobre la SMA20 y la SMA40 y la SMA20 subiendo.
  - Entra en 4 (Declive) con el cierre bajo la SMA20 y la SMA40 y la SMA20 bajando.
  - La 2 se mantiene hasta que el cierre pierde el último mínimo relevante o hay 3 cierres
    seguidos bajo la SMA20; entonces pasa a 3 (Distribución).
  - La 4 se mantiene hasta que el cierre supera el último máximo relevante o hay 3 cierres
    seguidos sobre la SMA20; entonces pasa a 1 (Acumulación).
  - Desde 1 o 3, la siguiente vela que cumpla las condiciones de 2 o de 4 entra en esa etapa.
  - Antes de la primera señal, «Rango» (0).
Máximo/mínimo relevante: el último pivote confirmado con la ventana del marco (indicators.pivots).
Con estas reglas salen las etapas de las capturas de MetaPro del 21-oct-2026 a las 20 h:
ETH 4/2/4 y BTC 1/2/4 (mensual/semanal/diario).
"""
import numpy as np
import pandas as pd

from . import indicators as ind

FAST, SLOW = 20, 40
CLOSES = 3          # cierres seguidos al otro lado de la SMA20 que terminan la etapa 2 o 4
NAMES = {0: "Rango", 1: "Acumulación", 2: "Avance", 3: "Distribución", 4: "Declive"}


def classify(df: pd.DataFrame, pivot_k: int) -> pd.DataFrame:
    """Etapa Power4 de cada vela, con las medias, el pivote vigente y la racha de cierres."""
    close = df["close"]
    s20, s40 = ind.sma(close, FAST), ind.sma(close, SLOW)
    st = ind.structure(df, pivot_k)
    # Pivote vigente antes de la vela: la propia vela no puede ser su máximo relevante
    ph, pl = st["last_ph"].shift(1).to_numpy(), st["last_pl"].shift(1).to_numpy()
    c, f, s, d = close.to_numpy(), s20.to_numpy(), s40.to_numpy(), s20.diff().to_numpy()
    n = len(df)
    stage, above, below = np.zeros(n, dtype=int), np.zeros(n, dtype=int), np.zeros(n, dtype=int)
    cur = a = b = 0
    for t in range(n):
        if np.isnan(s[t]):
            continue
        a = a + 1 if c[t] > f[t] else 0
        b = b + 1 if c[t] < f[t] else 0
        bull = c[t] > f[t] and c[t] > s[t] and d[t] > 0
        bear = c[t] < f[t] and c[t] < s[t] and d[t] < 0
        if cur == 2:
            if (not np.isnan(pl[t]) and c[t] < pl[t]) or b >= CLOSES:
                cur = 3
        elif cur == 4:
            if (not np.isnan(ph[t]) and c[t] > ph[t]) or a >= CLOSES:
                cur = 1
        elif bull:
            cur = 2
        elif bear:
            cur = 4
        stage[t], above[t], below[t] = cur, a, b
    return pd.DataFrame({"p4": stage, "sma20": s20, "sma40": s40, "sma20_diff": s20.diff(),
                         "ph": ph, "pl": pl, "above": above, "below": below}, index=df.index)


def _p(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4g}"


def reason(row: pd.Series, close: float) -> str:
    """Por qué está en esa etapa, en el estilo de MetaPro."""
    st = int(row["p4"])
    f, s = row["sma20"], row["sma40"]
    side = lambda m: "sobre" if close > m else "bajo"
    trend = "al alza" if row["sma20_diff"] > 0 else "a la baja" if row["sma20_diff"] < 0 else "plana"
    medias = (f"Precio {side(f)} la SMA20 ({_p(f)}) y {side(s)} la SMA40 ({_p(s)}); "
              f"la SMA20 va {trend}.")
    if st == 4:
        lvl = f" ({_p(row['ph'])})" if not pd.isna(row["ph"]) else ""
        return (f"Mantiene la etapa: no ha superado el último máximo relevante{lvl} ni tiene "
                f"{CLOSES} cierres sobre la SMA20 (lleva {int(row['above'])}). " + medias)
    if st == 2:
        lvl = f" ({_p(row['pl'])})" if not pd.isna(row["pl"]) else ""
        return (f"Mantiene la etapa: no ha perdido el último mínimo relevante{lvl} ni tiene "
                f"{CLOSES} cierres bajo la SMA20 (lleva {int(row['below'])}). " + medias)
    if st in (1, 3):
        return ("Pasa a 2 con el precio sobre la SMA20 y la SMA40 y la SMA20 al alza; "
                "a 4 con el precio bajo ambas y la SMA20 a la baja. " + medias)
    return "Aún sin señal de avance ni de declive. " + medias


def _entry(hist: pd.DataFrame, close: pd.Series) -> str:
    """Cómo salió de la etapa 2 o 4 la vela que abrió la 3 o la 1 actual."""
    st = hist["p4"].to_numpy()
    t = len(st) - 1
    while t > 0 and st[t - 1] == st[t]:
        t -= 1
    if t == 0:
        return ""
    row, c, when = hist.iloc[t], close.iloc[t], hist.index[t].strftime("%Y-%m-%d")
    if st[t] == 1:
        how = (f"superó el último máximo relevante ({_p(row['ph'])})"
               if not pd.isna(row["ph"]) and c > row["ph"] else f"hizo {CLOSES} cierres sobre la SMA20")
        return f"Salió de la etapa 4 en la vela del {when}: {how}. "
    how = (f"perdió el último mínimo relevante ({_p(row['pl'])})"
           if not pd.isna(row["pl"]) and c < row["pl"] else f"hizo {CLOSES} cierres bajo la SMA20")
    return f"Salió de la etapa 2 en la vela del {when}: {how}. "


def latest(df: pd.DataFrame, pivot_k: int) -> tuple[int | None, str]:
    """(etapa, motivo) de la última vela cerrada; (None, '') sin velas para la SMA40."""
    if len(df) < SLOW:
        return None, ""
    hist = classify(df, pivot_k)
    row = hist.iloc[-1]
    why = reason(row, float(df["close"].iloc[-1]))
    if row["p4"] in (1, 3):
        why = _entry(hist, df["close"]) + why
    return int(row["p4"]), why
