"""Test 1: la clasificación recorre el ciclo 1 → 2 → 3 → 4 → 1 sobre una serie sintética."""
import pytest

from etapas.classifier import classify
from etapas.config import TIMEFRAMES
from synth import cycle_for, segments

SEEDS = [1, 7, 42]
MIN_ACCURACY = 0.80


def _margin(cfg):
    """Velas excluidas al inicio de cada tramo: media longitud de la media clave.

    Una SMA de L velas tarda del orden de L/2 en reflejar un cambio de régimen, así que esas
    velas de frontera no se pueden exigir a ningún clasificador basado en la media.
    """
    return cfg.ma // 2


@pytest.mark.parametrize("tf", ["1d", "1w", "1M"])
@pytest.mark.parametrize("seed", SEEDS)
def test_accuracy_per_segment(tf, seed):
    cfg = TIMEFRAMES[tf]
    df, labels = cycle_for(cfg, seed)
    stage = classify(df, cfg)["stage"].to_numpy()
    m = _margin(cfg)
    for label, a, b in segments(labels):
        if label == 0:          # calentamiento
            continue
        acc = (stage[a + m:b] == label).mean()
        assert acc >= MIN_ACCURACY, f"{tf} semilla {seed}: tramo etapa {label} [{a},{b}) acierto {acc:.0%}"


@pytest.mark.parametrize("tf", ["1d", "1w", "1M"])
@pytest.mark.parametrize("seed", SEEDS)
def test_cycle_order(tf, seed):
    """Desde la primera base (tras el margen de frontera) y descartando los parpadeos
    (tramos más cortos que el margen), las etapas
    aparecen exactamente en el orden 1 → 2 → 3 → 4 → 1."""
    cfg = TIMEFRAMES[tf]
    df, labels = cycle_for(cfg, seed)
    stage = classify(df, cfg)["stage"].to_numpy()
    first_base = next(a for label, a, _ in segments(labels) if label == 1)
    runs = [(s, b - a) for s, a, b in segments(stage[first_base + _margin(cfg):])]
    stable = [s for s, length in runs if length >= _margin(cfg)]
    merged = [s for i, s in enumerate(stable) if i == 0 or s != stable[i - 1]]
    assert merged == [1, 2, 3, 4, 1], f"{tf} semilla {seed}: secuencia {merged}"


def test_dominant_stage_per_segment():
    """La etapa mayoritaria de cada tramo es la esperada (diario, semilla 7)."""
    cfg = TIMEFRAMES["1d"]
    df, labels = cycle_for(cfg, 7)
    stage = classify(df, cfg)["stage"].to_numpy()
    dominant = []
    for label, a, b in segments(labels):
        if label:
            values, counts = zip(*sorted({s: (stage[a:b] == s).sum() for s in (1, 2, 3, 4)}.items()))
            dominant.append(values[counts.index(max(counts))])
    assert dominant == [1, 2, 3, 4, 1]
