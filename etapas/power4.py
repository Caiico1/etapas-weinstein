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

Ajustes del 17-nov-2026, con la portada de MetaPro y las tarjetas de cada marco (BTC, ETH, SOL, UNI,
JUP y AAVE en mensual, semanal, diario y 6H): coinciden las 24 casillas (antes 15 de 18).
  - Datos: los cierres de MetaPro son los de Binance al céntimo. Semanal, diario y 6H usan velas
    cerradas; la SMA20 semanal que cita es la de velas cerradas (BTC 73.915, ETH 2.227).
  - Mensual: cuenta el mes en curso hasta el último cierre diario («Cierre Mensual» = cierre de
    ayer). Así BTC pasó de 1 a 4 en noviembre sin que cerrara ninguna vela mensual.
  - Con menos de 40 velas no hay SMA40 ni etapa confirmada: «Posible E4 (incipiente)» o E2 según
    la pendiente de la SMA20 (JUP mensual).
  - Salida de 4 a 1 también con el cierre sobre la SMA20 y la SMA40 y la SMA20 sin bajar («media de
    20 plana… se mantiene el contexto bajista previo», JUP 6H). Simétrico de 2 a 3.
  - De 1 a 4 hace falta, además de las condiciones de 4, cerrar bajo el mínimo de la vela que abrió
    la etapa 1 (BTC mensual lo perdió, 74.968; SOL mensual no, 70,58). Simétrico de 3 a 2.
"""
import numpy as np
import pandas as pd

from . import indicators as ind

FAST, SLOW = 20, 40
CLOSES = 3          # cierres seguidos al otro lado de la SMA20 que terminan la etapa 2 o 4
LIVE = {"1M"}       # marcos en que MetaPro cuenta la vela en curso
NAMES = {0: "Rango", 1: "Acumulación", 2: "Avance", 3: "Distribución", 4: "Declive"}


def classify(df: pd.DataFrame, pivot_k: int) -> pd.DataFrame:
    """Etapa Power4 de cada vela, con las medias, el pivote vigente y la racha de cierres."""
    close = df["close"]
    s20, s40 = ind.sma(close, FAST), ind.sma(close, SLOW)
    st = ind.structure(df, pivot_k)
    # Pivote vigente antes de la vela: la propia vela no puede ser su máximo relevante
    ph, pl = st["last_ph"].shift(1).to_numpy(), st["last_pl"].shift(1).to_numpy()
    c, f, s, d = close.to_numpy(), s20.to_numpy(), s40.to_numpy(), s20.diff().to_numpy()
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    stage, above, below = np.zeros(n, dtype=int), np.zeros(n, dtype=int), np.zeros(n, dtype=int)
    how = np.full(n, "", dtype=object)
    base = np.full(n, np.nan)   # en 1: mínimo de la vela que abrió la etapa; en 3: su máximo
    cur = a = b = 0
    lvl = np.nan
    for t in range(n):
        if np.isnan(s[t]):
            continue
        a = a + 1 if c[t] > f[t] else 0
        b = b + 1 if c[t] < f[t] else 0
        over, under = c[t] > f[t] and c[t] > s[t], c[t] < f[t] and c[t] < s[t]
        bull, bear = over and d[t] > 0, under and d[t] < 0
        prev, why = cur, ""
        if cur == 2:
            if not np.isnan(pl[t]) and c[t] < pl[t]:
                cur, why = 3, "min"
            elif b >= CLOSES:
                cur, why = 3, "closes"
            elif under and d[t] <= 0:
                cur, why = 3, "medias"
        elif cur == 4:
            if not np.isnan(ph[t]) and c[t] > ph[t]:
                cur, why = 1, "max"
            elif a >= CLOSES:
                cur, why = 1, "closes"
            elif over and d[t] >= 0:
                cur, why = 1, "medias"
        elif cur == 1:
            if bull:
                cur, why = 2, "avance"
            elif bear and c[t] < lvl:
                cur, why = 4, "base"
        elif cur == 3:
            if bear:
                cur, why = 4, "declive"
            elif bull and c[t] > lvl:
                cur, why = 2, "base"
        elif bull:
            cur, why = 2, "avance"
        elif bear:
            cur, why = 4, "declive"
        if cur != prev:
            lvl = lo[t] if cur == 1 else hi[t] if cur == 3 else np.nan
        stage[t], above[t], below[t], how[t], base[t] = cur, a, b, why, lvl
    return pd.DataFrame({"p4": stage, "sma20": s20, "sma40": s40, "sma20_diff": s20.diff(),
                         "ph": ph, "pl": pl, "above": above, "below": below, "how": how,
                         "base": base}, index=df.index)


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
    if st == 1:
        return (f"Pasa a 2 con el precio sobre la SMA20 y la SMA40 y la SMA20 al alza; a 4 con el "
                f"precio bajo ambas, la SMA20 a la baja y un cierre bajo el mínimo de la vela que abrió "
                f"la etapa 1 ({_p(row['base'])}). " + medias)
    if st == 3:
        return (f"Pasa a 4 con el precio bajo la SMA20 y la SMA40 y la SMA20 a la baja; a 2 con el "
                f"precio sobre ambas, la SMA20 al alza y un cierre sobre el máximo de la vela que abrió "
                f"la etapa 3 ({_p(row['base'])}). " + medias)
    return "Aún sin señal de avance ni de declive. " + medias


def _entry(hist: pd.DataFrame, close: pd.Series) -> str:
    """Cómo salió de la etapa 2 o 4 la vela que abrió la 3 o la 1 actual."""
    st = hist["p4"].to_numpy()
    t = len(st) - 1
    while t > 0 and st[t - 1] == st[t]:
        t -= 1
    if t == 0:
        return ""
    row, when = hist.iloc[t], hist.index[t].strftime("%Y-%m-%d")
    if st[t] == 1:
        how = {"max": f"superó el último máximo relevante ({_p(row['ph'])})",
               "closes": f"hizo {CLOSES} cierres sobre la SMA20",
               "medias": "cerró sobre la SMA20 y la SMA40 con la SMA20 sin bajar"}.get(row["how"], "")
        return f"Salió de la etapa 4 en la vela del {when}: {how}. " if how else ""
    how = {"min": f"perdió el último mínimo relevante ({_p(row['pl'])})",
           "closes": f"hizo {CLOSES} cierres bajo la SMA20",
           "medias": "cerró bajo la SMA20 y la SMA40 con la SMA20 sin subir"}.get(row["how"], "")
    return f"Salió de la etapa 2 en la vela del {when}: {how}. " if how else ""


def divergence(stage: int | None, p4: int | None, close: float | None = None,
               invalid: float | None = None) -> str | None:
    """Aviso cuando Power4 va contra la tendencia de Weinstein: Weinstein en 2 y Power4 en 3 o 4,
    o Weinstein en 4 y Power4 en 1 o 2. Power4, con medias más cortas, suele girar antes; es el
    momento de revisar, no una señal de cambio (Power4 no predijo mejor en la validación)."""
    if stage == 2 and p4 in (3, 4):
        word, cross, trend = "Alcista", "bajo", "bajista"
    elif stage == 4 and p4 in (1, 2):
        word, cross, trend = "Bajista", "sobre", "alcista"
    else:
        return None
    seen = (f"Power4 ya ve tendencia {trend}" if p4 in (2, 4)
            else f"Power4 ya da por terminada la etapa {stage}")
    text = f"Discrepancia: Weinstein en {stage} ({word}) y Power4 en {p4} ({NAMES[p4]}): {seen}."
    if invalid is not None and close:
        text += (f" La etapa {stage} de Weinstein se invalida con un cierre {cross} {_p(invalid)} "
                 f"(a {abs(invalid / close - 1):.1%} del cierre).")
    return text


def incipient(close: pd.Series) -> tuple[int, str]:
    """Sin velas para la SMA40 no hay etapa confirmada: «posible» 4 o 2 según la pendiente de la SMA20."""
    s20 = ind.sma(close, FAST)
    f, d, c = s20.iloc[-1], s20.iloc[-1] - s20.iloc[-2], close.iloc[-1]
    if np.isnan(d) or d == 0:
        return 0, f"Sin etapa: menos de {SLOW} velas y la SMA20 plana."
    st, word = (4, "bajista") if d < 0 else (2, "alcista")
    side = "sobre" if c > f else "bajo"
    return st, (f"Posible E{st} (incipiente): media de 20 {word}, estructura aún sin confirmar "
                f"(menos de {SLOW} velas, sin SMA40). Precio {side} la SMA20 ({_p(f)}).")


def latest(df: pd.DataFrame, pivot_k: int, current: pd.Series | None = None) -> tuple[int | None, str]:
    """(etapa, motivo) de la última vela cerrada, o de la vela en curso si se pasa `current` (los
    marcos de LIVE); (None, '') con menos de FAST velas."""
    if current is not None:
        df = pd.concat([df, current.to_frame().T.astype(float)])
    if len(df) <= FAST:
        return None, ""
    if len(df) < SLOW:
        return incipient(df["close"])
    hist = classify(df, pivot_k)
    row = hist.iloc[-1]
    why = reason(row, float(df["close"].iloc[-1]))
    if row["p4"] in (1, 3):
        why = _entry(hist, df["close"]) + why
    return int(row["p4"]), why
