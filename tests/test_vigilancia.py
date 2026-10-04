"""Vigilancia de oportunidades: lectura de oportunidades.txt, estado y avisos (sin red)."""
import json

import pandas as pd

from etapas import oportunidad as op
from etapas import rutina, vigilancia
from etapas.pools import Pool
from etapas.liquidez import PoolQuote
from etapas.vigilancia import Watch, alerts, check, load_watches

POOL = Pool("base-uniswap4-x", "BTC/USDC", "Uniswap v4", "base", "0x" + "ab" * 32, "USDC/cbBTC", 0.0005, 10, 6, 8, False)


def test_load_watches(tmp_path):
    f = tmp_path / "oportunidades.txt"
    f.write_text("# vigilancias\nBTC/USDC 5000 base\ncbbtc/usdc 1.000,5 base, arbitrum  # nota\nETH/USDC 5000\n"
                 "ETH/USDC 5000 marte\nUSDC/USDT 5000\nsolo\n", encoding="utf-8")
    watches, warnings = load_watches(f)
    assert [(w.pair, w.capital, w.chains) for w in watches] == [
        ("BTC/USDC", 5000.0, ("base",)), ("BTC/USDC", 1000.5, ("base", "arbitrum")), ("ETH/USDC", 5000.0, ())]
    assert watches[0].key == "BTC/USDC 5000 base" and watches[2].key == "ETH/USDC 5000 todas"
    assert len(warnings) == 3
    assert load_watches(tmp_path / "no.txt") == ([], [])


def _report(proposal: bool):
    rep = op.Report("BTC", "USDC", 5000, 85000.0, 1.0, None)
    o = op.Opportunity("1w", "Semanal", 7.0, "favorable", [], POOL,
                       PoolQuote(POOL, 78000.0, 91000.0, 1, 2, 85000.0), op.Backtest(0.87, -0.005, 104))
    o.net_cycle, o.net_safe, o.net_apr = (12.0, 5.0, 0.125) if proposal else (-4.25, -10.7, -0.044)
    o.excluded = "" if proposal else "resultado neto estimado negativo"
    rep.opportunities = [o]
    rep.proposals = [o] if proposal else []
    return rep


def test_check_with_and_without_proposal():
    w = Watch("BTC/USDC", 5000, ("base",))
    seen = []
    yes = check(w, lambda pair, capital, chains: seen.append((pair, capital, chains)) or _report(True))
    assert seen == [("BTC/USDC", 5000, ("base",))]
    assert yes["hay_propuesta"] and yes["estado"] == "Semanal · " + POOL.label
    assert "78,000" in yes["texto"] and "+12.00 $" in yes["texto"] and yes["enlace"].startswith("https://app.uniswap.org")
    no = check(w, lambda *a: _report(False))
    assert not no["hay_propuesta"] and no["estado"] == "" and "-4.25 $" in no["texto"]

    def boom(*a):
        raise OSError("sin red")
    failed = check(w, boom)
    assert failed["error"] and not failed["hay_propuesta"]
    bad = check(Watch("BTC/USDC", 5000), lambda *a: op.Report("X", "", 5000, None, None, None, error="par no admitido"))
    assert bad["error"] and "par no admitido" in bad["texto"]


def test_alerts_only_on_change():
    w = Watch("BTC/USDC", 5000, ("base",))
    yes, no = check(w, lambda *a: _report(True)), check(w, lambda *a: _report(False))
    assert alerts({}, [no]) == []                                        # nunca hubo propuesta: silencio
    first = alerts({}, [yes])
    assert len(first) == 1 and first[0]["tipo"] == "oportunidad" and "propuesta: ciclo semanal" in first[0]["texto"]
    assert alerts({w.key: yes["estado"]}, [yes]) == []                   # sigue igual: sin aviso
    gone = alerts({w.key: yes["estado"]}, [no])
    assert len(gone) == 1 and "ya no cumple" in gone[0]["texto"]
    assert alerts({w.key: ""}, [no]) == []


def test_routine_watches(tmp_path, monkeypatch):
    from test_liquidez import _asset
    monkeypatch.setattr(rutina, "analyze_symbol", lambda symbol, provisional=False: _asset(symbol))
    monkeypatch.setattr(vigilancia, "check", lambda w: check(w, lambda *a: _report(True)))
    (tmp_path / "watchlist.txt").write_text("ETH\n", encoding="utf-8")
    (tmp_path / "oportunidades.txt").write_text("BTC/USDC 5000 base\n", encoding="utf-8")
    out = tmp_path / "out"
    assert rutina.run(tmp_path / "watchlist.txt", out, now=pd.Timestamp("2026-10-04 06:00", tz="UTC")) == 0
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Oportunidades de liquidez vigiladas" in index and "hay propuesta" in index
    assert "propuesta: ciclo semanal" in (out / "alertas.md").read_text(encoding="utf-8")
    hist = json.loads((out / "historial" / "2026-10-04.json").read_text(encoding="utf-8"))
    assert hist["oportunidades"] == {"BTC/USDC 5000 base": "Semanal · " + POOL.label}
    # Al día siguiente, con la misma propuesta, no hay aviso nuevo
    assert rutina.run(tmp_path / "watchlist.txt", out, now=pd.Timestamp("2026-10-05 06:00", tz="UTC")) == 0
    assert not (out / "alertas.md").exists()


def test_non_notifying_run_does_not_consume_the_alert(tmp_path, monkeypatch):
    """Una propuesta que aparece en una ejecución sin correo (ETAPAS_AVISA=0) no se da por avisada:
    la siguiente ejecución con correo la incluye."""
    from test_liquidez import _asset
    monkeypatch.setattr(rutina, "analyze_symbol", lambda symbol, provisional=False: _asset(symbol))
    monkeypatch.setattr(vigilancia, "check", lambda w: check(w, lambda *a: _report(True)))
    (tmp_path / "watchlist.txt").write_text("ETH\n", encoding="utf-8")
    (tmp_path / "oportunidades.txt").write_text("BTC/USDC 5000 base\n", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setenv("ETAPAS_AVISA", "0")
    assert rutina.run(tmp_path / "watchlist.txt", out, now=pd.Timestamp("2026-10-04 12:00", tz="UTC")) == 0
    hist = json.loads((out / "historial" / "2026-10-04.json").read_text(encoding="utf-8"))
    assert hist["oportunidades"] == {}                                   # no se da por avisada
    monkeypatch.setenv("ETAPAS_AVISA", "1")
    assert rutina.run(tmp_path / "watchlist.txt", out, now=pd.Timestamp("2026-10-05 06:00", tz="UTC")) == 0
    assert "propuesta: ciclo semanal" in (out / "alertas.md").read_text(encoding="utf-8")
    hist = json.loads((out / "historial" / "2026-10-05.json").read_text(encoding="utf-8"))
    assert hist["oportunidades"] == {"BTC/USDC 5000 base": "Semanal · " + POOL.label}
