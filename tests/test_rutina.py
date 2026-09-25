"""Lista de seguimiento, detección de cambios y rutina completa (sin red)."""
import json

import pandas as pd

from etapas import rutina
from etapas.analysis import AssetResult, TimeframeResult, analyze_candles
from etapas.config import TIMEFRAMES
from etapas.data import Candles
from synth import cycle_for


def test_load_watchlist(tmp_path):
    f = tmp_path / "watchlist.txt"
    f.write_text("# comentario\nbtc\n\nETH  # la segunda\nBTC\n# SOL\nno vale!\n", encoding="utf-8")
    symbols, warnings = rutina.load_watchlist(f)
    assert symbols == ["BTC", "ETH"]
    assert any("repetido" in w for w in warnings)
    assert any("no parece un símbolo" in w for w in warnings)


def test_diff_detects_stage_changes_and_list_changes():
    prev = {"BTC": {"1w": {"etapa": 4, "transicion": "", "vela": "x"},
                    "1d": {"etapa": 1, "transicion": "", "vela": "x"}},
            "XRP": {"1d": {"etapa": 2, "transicion": "", "vela": "x"}}}
    curr = {"BTC": {"1w": {"etapa": 1, "transicion": "", "vela": "y"},
                    "1d": {"etapa": 1, "transicion": "1→2", "vela": "y"}},
            "ADA": {"1d": {"etapa": 2, "transicion": "", "vela": "y"}}}
    texts = {(c["simbolo"], c["marco"], c["texto"]) for c in rutina.diff(prev, curr)}
    assert ("BTC", "Semanal", "etapa 4 → 1") in texts
    assert ("BTC", "Diario", "entra en transición 1→2") in texts
    assert ("ADA", "", "añadido a la lista") in texts
    assert ("XRP", "", "retirado de la lista") in texts


def _fake_analyze(symbol, provisional=False):
    if symbol == "MALO":
        err = "No se pudieron descargar datos. binance: fallo | kraken: fallo"
        return AssetResult(symbol, {k: TimeframeResult(k, TIMEFRAMES[k].label, "error", err)
                                    for k in TIMEFRAMES}, err)
    results = {}
    for k, cfg in TIMEFRAMES.items():
        df, _ = cycle_for(cfg, seed=1)
        results[k] = analyze_candles(Candles(df, None, "sintético"), cfg)
    return AssetResult(symbol, results)


def test_routine_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(rutina, "analyze_symbol", _fake_analyze)
    lista = tmp_path / "watchlist.txt"
    lista.write_text("AAA\nMALO\n", encoding="utf-8")
    out = tmp_path / "out"

    # Instantánea "de ayer" con otra etapa, para forzar un cambio
    hist = out / "historial"
    hist.mkdir(parents=True)
    (hist / "2026-09-24.json").write_text(json.dumps(
        {"fecha": "2026-09-24", "activos": {"AAA": {"1d": {"etapa": 9, "transicion": "", "vela": ""}}}}),
        encoding="utf-8")

    code = rutina.run(lista, out, now=pd.Timestamp("2026-09-25 06:00", tz="UTC"))
    assert code == 0
    assert (out / "etapas_AAA.html").exists()
    assert not (out / "etapas_MALO.html").exists()
    assert (hist / "2026-09-25.json").exists()
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Cambios desde 2026-09-24" in index and "etapa 9 →" in index
    assert "MALO" in index and "No se pudieron descargar" in index
    assert 'href="etapas_AAA.html"' in index
    assert "Todos los activos" in (out / "etapas_AAA.html").read_text(encoding="utf-8")
    log = (out / "rutina.log").read_text(encoding="utf-8")
    assert "1/2 activos analizados" in log and "AVISO MALO" in log


def test_routine_missing_or_empty_list(tmp_path):
    assert rutina.run(tmp_path / "no_existe.txt", tmp_path / "out") == 1
    vacia = tmp_path / "vacia.txt"
    vacia.write_text("# nada\n", encoding="utf-8")
    assert rutina.run(vacia, tmp_path / "out") == 1
