"""Series sintéticas para los tests. Solo se usan en tests, nunca en la herramienta."""
import numpy as np
import pandas as pd


def ohlcv_from_close(close: np.ndarray, rng: np.random.Generator, wick: float = 0.006,
                     start: str = "2018-01-01", freq: str = "D") -> pd.DataFrame:
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, wick, len(close))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, wick, len(close))))
    volume = 1000 * (1 + 0.3 * np.abs(rng.normal(0, 1, len(close))))
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": volume}, index=idx)


def make_cycle(seed: int = 7, seg: int = 300, warm: int = 200, noise: float = 0.012,
               period: int = 20, amp: float = 0.05, ar: float = 0.8, freq: str = "D"):
    """Ciclo completo: (caída de calentamiento) → base → subida → techo → caída → base.

    Devuelve (df, etiquetas). La etiqueta 0 marca el tramo de calentamiento, que solo existe
    para que la primera base tenga una caída previa, como exige la definición de etapa 1.
    """
    rng = np.random.default_rng(seed)
    low, high = np.log(100), np.log(300)
    t = np.arange(seg)
    osc = amp * np.sin(2 * np.pi * t / period)       # oscilación del rango
    parts = [
        (0, np.linspace(np.log(200), low, warm)),
        (1, low + osc),
        (2, np.linspace(low, high, seg)),
        (3, high + osc),
        (4, np.linspace(high, low, seg)),
        (1, low + osc),
    ]
    path = np.concatenate([p for _, p in parts])
    labels = np.concatenate([np.full(len(p), s) for s, p in parts])
    e = np.zeros(len(path))
    shocks = rng.normal(0, noise, len(path))
    for i in range(1, len(path)):
        e[i] = ar * e[i - 1] + shocks[i]
    close = np.exp(path + e)
    return ohlcv_from_close(close, rng, freq=freq), labels


def cycle_for(cfg, seed: int):
    """Ciclo sintético escalado al marco temporal `cfg`.

    - Cada tramo dura 6 × la media clave, y el margen de calentamiento 4 × la media.
    - La oscilación del rango tiene un periodo de 0,4 × la media: más corta que la media, para
      que sea un rango en la escala del marco (si dura tanto como la media, la propia media
      la sigue y cada vaivén es una mini tendencia según las definiciones de etapa).
    - El ruido se ajusta para que la tendencia sea igual de clara en todos los marcos: la
      pendiente de la media en la ventana de pendiente vale ≈ 3,5 veces el umbral de "plana".
    """
    ma = cfg.ma
    seg = 6 * ma
    noise = np.log(3) / seg * 2 * cfg.slope_window / 6.1
    return make_cycle(seed=seed, seg=seg, warm=4 * ma, noise=noise,
                      period=round(0.4 * ma), amp=0.05)


def segments(labels):
    """[(etiqueta, inicio, fin)] de tramos consecutivos."""
    out, i = [], 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        out.append((int(labels[i]), i, j))
        i = j
    return out
