"""Cribado de duplas: fase 1 con datos de DefiLlama falsos y fase 2 simulada (sin red)."""
import pytest

from etapas import cribado as C
from etapas import oportunidad as op
from etapas import pools as P

WETH_B = "0x4200000000000000000000000000000000000006"
USDC_B = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
UNKNOWN = "0x07b3d902783c3c12b077508c3b5c00113d1291d0"


def _item(symbol, toks, tvl, vol7, meta="0.05%", chain="Base", project="uniswap-v3", count=400):
    return {"chain": chain, "project": project, "symbol": symbol, "tvlUsd": tvl, "poolMeta": meta,
            "volumeUsd7d": vol7, "volumeUsd1d": vol7 / 7, "underlyingTokens": toks, "count": count,
            "apyBase": None, "apyBase7d": None}


DATA = [
    _item("WETH-USDC", [WETH_B, USDC_B], 10e6, 7 * 30e6),                 # 0,15 %/día → pasa
    _item("CBBTC-USDC", [CBBTC, USDC_B], 10e6, 7 * 10e6),                 # 0,05 %/día → no llega
    _item("XDP-USDC", [UNKNOWN, USDC_B], 2e6, 7 * 200e6, meta="0.01%"),   # sin verificar y sospechoso
    _item("WETH-CBBTC", [WETH_B, CBBTC], 5e6, 7 * 20e6, count=30),        # pasa el criterio pero es joven
    _item("WETH-USDC", [WETH_B, USDC_B], 5e6, 7 * 50e6, meta="Dynamic fee (hook)", project="uniswap-v4"),
    _item("WETH-USDC", [WETH_B, USDC_B], 0.5e6, 7 * 50e6),                # TVL < 1 M$
    _item("SOL-USDC", ["So1", "x"], 9e6, 7 * 9e6, chain="Solana", project="raydium"),   # DEX no admitido
]


@pytest.fixture
def llama(monkeypatch):
    monkeypatch.setattr(P, "_http_json", lambda url, payload=None: {"data": DATA})


def test_phase1_filters_and_metrics(llama):
    w = []
    cands = C.fetch_candidates(["base", "solana"], w)
    assert not w and len(cands) == 5                         # sin el de poco TVL ni el DEX no admitido
    eth = next(c for c in cands if c.name == "WETH-USDC 0.05%")
    assert eth.pair == "ETH/USD" and eth.week
    assert eth.vol_tvl == pytest.approx(3.0) and eth.fees_tvl == pytest.approx(0.0015)
    xdp = next(c for c in cands if c.name.startswith("XDP"))
    assert not xdp.verified and any("sospechoso" in f for f in xdp.flags())
    young = next(c for c in cands if c.name.startswith("WETH-CBBTC"))
    assert not young.eligible and any("historial" in f for f in young.flags())
    hook = next(c for c in cands if c.hooked)
    assert not hook.eligible
    assert C.shortlist(cands, 0.001, 5) == ["ETH/USD"]
    assert C.shortlist(cands, 0.0004, 5) == ["ETH/USD", "BTC/USD"]


def test_full_run_ranks_only_pairs_that_pass_pool(llama, monkeypatch):
    def fake_analyze(pair, capital, chains):
        rep = op.Report(*pair.split("/"), capital, 1.0, 1.0, None)
        if pair == "ETH/USD":
            o = op.Opportunity("1w", "Semanal", 7.0, "apta", [], None, None, op.Backtest(0.8, -0.01, 50))
            o.net_cycle, o.net_safe, o.net_apr = 20.0, 15.0, 0.2
            rep.proposals = [o]
        return rep
    monkeypatch.setattr(op, "analyze", fake_analyze)
    monkeypatch.setattr(op, "checklist", lambda rep, o: ([], ""))
    monkeypatch.setattr(op, "to_json", lambda rep: {"propuestas": [{"ciclo": "Semanal"}]})
    s = C.run(5000, ["base"], top=5, min_fees=0.0004)
    assert set(s.reports) == {"ETH/USD", "BTC/USD"}
    assert [p for p, _ in C.ranking(s)] == ["ETH/USD"]


def test_render_without_phase_two(llama):
    s = C.run(5000, ["base"], deep=False)
    text = C.render(s)
    assert "Pares que pasan a la fase 2" in text and "**ETH/USD**" in text
    assert "Fuera de la lista verificada" in text and "volumen sospechoso" in text
    assert C.to_json(s)["recomendadas"] == []


def test_config_for_the_daily_routine(tmp_path, llama):
    assert C.load_config(tmp_path / "no.txt") is None
    f = tmp_path / "cribado.txt"
    f.write_text("# comentario\n4500 base, arbitrum,marte\n", encoding="utf-8")
    assert C.load_config(f) == (4500.0, ["base", "arbitrum"])
    f.write_text("5000\n", encoding="utf-8")
    assert C.load_config(f) == (5000.0, None)
    C.save(C.run(5000, ["base"], deep=False), tmp_path / "out")
    assert (tmp_path / "out" / "cribado.md").exists() and (tmp_path / "out" / "cribado.json").exists()


def test_llama_failure_is_reported(monkeypatch):
    def boom(url, payload=None):
        raise OSError("sin red")
    monkeypatch.setattr(P, "_http_json", boom)
    s = C.run(5000, deep=False)
    assert s.warnings and "DefiLlama" in s.warnings[0] and not s.shortlist
