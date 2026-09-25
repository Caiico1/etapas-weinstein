"""Clasificación en etapas de Weinstein mediante reglas explícitas y puntuación ponderada.

Cada señal reparte entre 0 y 1 punto a cada etapa; la puntuación final (0–100) es la suma
ponderada según config.WEIGHTS. Las señales "de rango" (media plana, precio cruzando la media,
estructura en contracción) valen igual para la etapa 1 y la 3: la tendencia previa decide a
cuál de las dos van, porque es lo único que las distingue.
"""
import numpy as np
import pandas as pd

from . import indicators as ind
from .config import (CONF_HIGH, CONF_MEDIUM, FLAT_ATR_FACTOR, PRIOR_RUN, PRIOR_SIGNIFICANCE,
                     VOLUME_SPIKE, WEIGHTS, TimeframeConfig)

STAGE_NAMES = {1: "Inicio (base)", 2: "Alcista", 3: "Distribución", 4: "Bajista"}
NEXT_STAGE = {1: 2, 2: 3, 3: 4, 4: 1}
STAGES = (1, 2, 3, 4)
SLOPE_SOFTNESS = 1.0   # anchura de la transición plana ↔ con pendiente, en unidades de z
RANGE_CROSSES = 2      # cruces en N velas que cuentan como rango pleno
TREND_GATE = 1.0       # atenuación de posición/estructura para 2 y 4 con media plana
PERSIST_MIN = 1.0      # velas seguidas (× ventana de pendiente) para que la persistencia cuente
FAR_ATR = 3.0          # distancia a la media (en ATR) a partir de la cual no hay rango
SCORE_SMOOTHING = 1.0  # suavizado de la puntuación, en fracción de la ventana de pendiente


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


def _run_length(ma: pd.Series) -> pd.Series:
    """Velas seguidas que la media lleva subiendo (+) o bajando (−)."""
    step = np.sign(ma.diff()).fillna(0).to_numpy()
    run = np.zeros(len(step))
    for t in range(1, len(step)):
        if step[t] != 0 and step[t] == np.sign(run[t - 1]):
            run[t] = run[t - 1] + step[t]
        else:
            run[t] = step[t]
    return pd.Series(run, index=ma.index)


def _prior_trend(ma: np.ndarray, atr_pct: np.ndarray, z: np.ndarray, cfg: TimeframeConfig):
    """Última tendencia significativa de la media antes de la vela actual.

    Una vela es parte de una tendencia significativa si se cumple cualquiera de estas dos
    condiciones:
      A) Magnitud: la media cambió en la ventana previa (MA_t / MA_{t−W} − 1) más de
         PRIOR_SIGNIFICANCE × ATR% × √W. El ruido aleatorio crece con la raíz de la ventana,
         así que el mismo umbral sirve para diario, semanal y mensual.
      B) Persistencia: la media lleva al menos PRIOR_RUN × ventana de pendiente velas seguidas
         con pendiente no plana en la misma dirección.
    El "ancla" es la última vela que cumple A o B: el final de la última tendencia real, justo
    antes de que empezara el aplanamiento. Mientras la media siga plana, el ancla no se mueve,
    así que una base larga sigue "recordando" la caída que la precedió.

    Devuelve (cambio de la media en la ventana previa al ancla, índice del ancla, dirección).
    """
    n, w = len(ma), cfg.prior_window
    min_run = PRIOR_RUN * cfg.slope_window
    prior = np.full(n, np.nan)
    direction = np.zeros(n)
    anchors = np.full(n, -1)
    anchor, anchor_move, anchor_dir, first_valid = -1, np.nan, 0.0, None
    run_len, run_sign = 0, 0
    for t in range(n):
        if np.isnan(ma[t]):
            continue
        if first_valid is None:
            first_valid = t
        sign = 0 if np.isnan(z[t]) or abs(z[t]) < 1 else int(np.sign(z[t]))
        run_len = run_len + 1 if sign != 0 and sign == run_sign else (1 if sign else 0)
        run_sign = sign
        start = max(t - w, first_valid)
        if start < t and not np.isnan(atr_pct[t]):
            move = ma[t] / ma[start] - 1
            if abs(move) > PRIOR_SIGNIFICANCE * atr_pct[t] * np.sqrt(t - start):
                anchor, anchor_move, anchor_dir = t, move, float(np.sign(move))
            elif run_len >= min_run:
                anchor, anchor_move, anchor_dir = t, move, float(sign)
        if anchor >= 0:
            prior[t], direction[t] = anchor_move, anchor_dir
        anchors[t] = anchor
    return prior, anchors, direction


def classify(df: pd.DataFrame, cfg: TimeframeConfig, ma_len: int | None = None) -> pd.DataFrame:
    """Calcula señales, puntuaciones y etapa para cada vela del histórico."""
    ma_len = ma_len or cfg.ma
    n, N = cfg.slope_window, cfg.position_window
    out = pd.DataFrame(index=df.index)
    close = df["close"]

    # 1. Pendiente de la media, normalizada por la volatilidad
    out["ma"] = ind.sma(close, ma_len)
    out["atr"] = ind.atr(df, cfg.atr)
    out["atr_pct"] = out["atr"] / close
    out["slope"] = ind.slope(out["ma"], n)
    out["flat_thr"] = (FLAT_ATR_FACTOR * out["atr_pct"]).clip(lower=1e-9)
    z = out["slope"] / out["flat_thr"]          # |z| < 1 ⇒ media plana
    out["z"] = z
    out["ma_run"] = _run_length(out["ma"])

    # 2. Posición del precio respecto a la media
    out["pct_above"] = ind.pct_above(close, out["ma"], N)
    out["crosses"] = ind.crosses(close, out["ma"], N)

    # 3. Estructura de pivotes
    st = ind.structure(df, cfg.pivot_window)
    out = out.join(st)

    # 4. Tendencia previa
    prior, anchors, direction = _prior_trend(out["ma"].to_numpy(), out["atr_pct"].to_numpy(),
                                             z.to_numpy(), cfg)
    out["prior_chg"] = prior
    out["prior_anchor"] = anchors
    # p_up: 1 si la tendencia previa fue alcista, 0 si bajista, 0.5 si no hay ninguna
    out["p_up"] = (direction + 1) / 2

    # 5. Rango (últimas N velas) y rupturas respecto al rango anterior
    out["range_top"] = df["high"].rolling(N, min_periods=N).max()
    out["range_bottom"] = df["low"].rolling(N, min_periods=N).min()
    brk_up = close > df["high"].rolling(N, min_periods=N).max().shift(1)
    brk_dn = close < df["low"].rolling(N, min_periods=N).min().shift(1)

    # 6. Volumen
    vol_ma = df["volume"].rolling(cfg.volume_ma, min_periods=cfg.volume_ma).mean()
    out["vol_ratio"] = df["volume"] / vol_ma.replace(0, np.nan)
    spike = out["vol_ratio"] > VOLUME_SPIKE
    out["breakout"] = np.where(brk_up, "alcista", np.where(brk_dn, "bajista", ""))

    # 7. Puntuación
    pu = out["p_up"].fillna(0.5)
    # Transición gradual centrada en el umbral |z| = 1: a esa altura la media es mitad
    # "plana" y mitad "con pendiente"; solo con |z| ≥ 1 + SLOPE_SOFTNESS cuenta del todo.
    flat = _clip01(0.5 - (z.abs() - 1) / (2 * SLOPE_SOFTNESS))
    up = _clip01(0.5 + (z - 1) / (2 * SLOPE_SOFTNESS))
    dn = _clip01(0.5 + (-z - 1) / (2 * SLOPE_SOFTNESS))
    # Persistencia: una media que lleva muchas velas seguidas en la misma dirección tiene
    # pendiente aunque su magnitud no llegue al umbral (típico del mensual, donde el ATR%
    # es enorme). Cuenta a partir de PERSIST_MIN × n velas seguidas y del todo con n más, y solo
    # si el precio lo confirma (media subiendo con precio encima, o bajando con precio debajo):
    # una media que sigue subiendo por retraso mientras el precio se hunde no es etapa 2.
    run = out["ma_run"]
    pers = _clip01((run.abs() - PERSIST_MIN * n) / n)
    up = np.maximum(up, pers * ((run > 0) & (close > out["ma"])))
    dn = np.maximum(dn, pers * ((run < 0) & (close < out["ma"])))
    flat = np.minimum(flat, 1 - pers)

    comp = {s: {} for s in STAGES}
    # Pendiente
    comp[1]["pendiente"] = flat * (1 - pu)
    comp[3]["pendiente"] = flat * pu
    comp[2]["pendiente"] = up
    comp[4]["pendiente"] = dn
    # Posición: muchos cruces o precio repartido a ambos lados ⇒ rango
    p = out["pct_above"]
    rng = 0.5 * _clip01(out["crosses"] / RANGE_CROSSES) + 0.5 * (1 - (2 * p - 1).abs())
    # Distancia a la media en ATR: más allá de FAR_ATR el precio no está en rango aunque la
    # media (que va con retraso) siga plana. "far" va de 0 (a 1 ATR) a 1 (a FAR_ATR ATR).
    out["dist_atr"] = (close / out["ma"] - 1) / out["atr_pct"].clip(lower=1e-9)
    far = _clip01((out["dist_atr"].abs() - 1) / (FAR_ATR - 1))
    rng = rng * (1 - far)
    comp[2]["posicion"] = p * (1 - rng)
    comp[4]["posicion"] = (1 - p) * (1 - rng)
    comp[1]["posicion"] = rng * (1 - pu)
    comp[3]["posicion"] = rng * pu
    # Estructura: HH/HL ⇒ 2, LH/LL ⇒ 4, mixta ⇒ rango (1 o 3)
    hh = out["hh"].map(lambda v: 0.5 if pd.isna(v) else float(v))
    hl = out["hl"].map(lambda v: 0.5 if pd.isna(v) else float(v))
    known = out["hh"].notna() | out["hl"].notna()
    s_up, s_dn = (hh + hl) / 2, ((1 - hh) + (1 - hl)) / 2
    s_rng = (1 - (s_up - s_dn).abs()).where(known, 0.0)
    comp[2]["estructura"] = (s_up - np.minimum(s_up, s_dn)).where(known, 0.0)
    comp[4]["estructura"] = (s_dn - np.minimum(s_up, s_dn)).where(known, 0.0)
    comp[1]["estructura"] = s_rng * (1 - pu)
    comp[3]["estructura"] = s_rng * pu
    # Tendencia previa: con media plana decide 1 vs 3; con pendiente, detecta el giro
    # (pendiente al alza tras caída ⇒ 1→2; pendiente a la baja tras subida ⇒ 3→4)
    nf_up = (1 - flat) * (z > 0)
    nf_dn = (1 - flat) * (z < 0)
    comp[1]["previa"] = flat * (1 - pu) + nf_up * (1 - pu)
    comp[2]["previa"] = nf_up * pu
    comp[3]["previa"] = flat * pu + nf_dn * pu
    comp[4]["previa"] = nf_dn * (1 - pu)
    # Volumen: solo confirma rupturas del rango
    comp[1]["volumen"] = comp[3]["volumen"] = pd.Series(0.0, index=df.index)
    comp[2]["volumen"] = (brk_up & spike).astype(float)
    comp[4]["volumen"] = (brk_dn & spike).astype(float)

    # Las etapas 2 y 4 exigen que la media tenga pendiente: con la media plana, la posición
    # y la estructura cuentan menos para ellas (TREND_GATE = fracción que se pierde).
    # Excepción: si el precio está lejos de la media, la posición cuenta entera.
    gate = 1 - TREND_GATE * flat
    for s in (2, 4):
        comp[s]["posicion"] = comp[s]["posicion"] * (gate + (1 - gate) * far)
        comp[s]["estructura"] = comp[s]["estructura"] * gate

    # Puntuación de la vela y puntuación suavizada (media de las últimas SCORE_SMOOTHING ×
    # ventana de pendiente velas), para que la etapa no cambie por una sola vela.
    smooth = max(1, round(SCORE_SMOOTHING * n))
    for s in STAGES:
        raw = 100 * sum(WEIGHTS[k] * comp[s][k] for k in WEIGHTS)
        out[f"raw_{s}"] = raw
        out[f"score_{s}"] = raw.rolling(smooth, min_periods=1).mean()

    valid = out[["ma", "slope", "pct_above", "atr"]].notna().all(axis=1)
    scores = out[[f"score_{s}" for s in STAGES]].to_numpy()
    order = np.argsort(-scores, axis=1, kind="stable")
    top, second = order[:, 0], order[:, 1]
    rows = np.arange(len(out))
    out["stage"] = np.where(valid, top + 1, 0)
    out["second"] = np.where(valid, second + 1, 0)
    out["confidence"] = np.where(valid, scores[rows, top] - scores[rows, second], np.nan)
    out["confidence_label"] = [confidence_label(c) for c in out["confidence"]]
    out["transition"] = [
        f"{s}→{s2}" if s and NEXT_STAGE[s] == s2 and c < CONF_HIGH else ""
        for s, s2, c in zip(out["stage"], out["second"], out["confidence"].fillna(0))
    ]
    out["slope_label"] = np.where(z.abs() < 1, "plana", np.where(z > 0, "positiva", "negativa"))
    out["close"] = close
    return out


def confidence_label(c: float) -> str:
    if c is None or np.isnan(c):
        return ""
    if c > CONF_HIGH:
        return "alta"
    if c >= CONF_MEDIUM:
        return "media"
    return "baja"


def _nearest(candidates, price, below: bool):
    """Nivel más cercano al precio por debajo (o por encima); si no hay, el más extremo."""
    vals = [v for v in candidates if v is not None and not np.isnan(v)]
    if not vals:
        return np.nan
    side = [v for v in vals if (v < price if below else v > price)]
    if side:
        return max(side) if below else min(side)
    return min(vals) if below else max(vals)


def levels(row: pd.Series) -> tuple[float, float]:
    """(nivel que confirma, nivel que invalida) la etapa actual."""
    s, price = int(row["stage"]), row["close"]
    if s == 1:
        return row["range_top"], row["range_bottom"]
    if s == 3:
        return row["range_bottom"], row["range_top"]
    if s == 2:
        confirm = row["last_ph"] if row["last_ph"] > price else row["range_top"]
        return confirm, _nearest([row["last_pl"], row["ma"]], price, below=True)
    if s == 4:
        confirm = row["last_pl"] if row["last_pl"] < price else row["range_bottom"]
        return confirm, _nearest([row["last_ph"], row["ma"]], price, below=False)
    return np.nan, np.nan
