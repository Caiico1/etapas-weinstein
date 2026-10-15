"""Aave V3: decodificación de los datos del PoolDataProvider, activos no listados y su uso como
alternativa en el análisis de liquidez (sin red)."""
import pytest

from etapas import aave as A
from etapas import oportunidad as op
from etapas.pools import FeeRates
from etapas import pools as P
from test_oportunidad import fake_world  # noqa: F401  (fixture)

# Palabras reales del USDC de Aave V3 en Ethereum (15-oct-2026)
CFG = [6, 7500, 7800, 10450, 1000, 1, 1, 0, 1, 0]
DATA = [0, 113622280217, 2375267522204962, 0, 2224001124498039, 37767895392771640264820371,
        44821095561752358720404578, 0, 0, 1189749174322563218918646019, 1253976026650670792690217459, 1792094723]
CAPS = [2700000000, 3000000000]


@pytest.fixture(autouse=True)
def _clean_cache():
    A._reserve_cache.clear()
    A._provider_cache.clear()
    yield
    A._reserve_cache.clear()
    A._provider_cache.clear()


def test_selectors_match_the_abi():
    assert A.SEL["data"] == "35ea6a75" and A.SEL["config"] == "3e150141"
    assert A.SEL["data_provider"] == "e860accb" and A.SEL["caps"] == "46fbe558"


def test_decode_real_usdc_reserve():
    r = A.decode("ethereum", "USDC", "0xA0b8", 6, CFG, DATA, CAPS, 0, 0)
    assert r.supply_apr == pytest.approx(0.037767895, rel=1e-6)
    assert r.supply_apy > r.supply_apr and r.supply_apy == pytest.approx(0.0385, abs=2e-4)
    assert r.borrow_apr == pytest.approx(0.044821095, rel=1e-6)
    assert r.utilization == pytest.approx(0.9363, abs=1e-4)
    assert r.supplied == pytest.approx(2_375_267_522.2, rel=1e-6)
    assert (r.ltv, r.liquidation_threshold) == (0.75, 0.78) and r.liquidation_bonus == pytest.approx(0.045)
    assert r.supply_cap == 3e9 and r.borrow_cap == 2.7e9
    assert not r.isolated and r.can_supply and A.status(r) == "normal"


def test_frozen_capped_and_isolated_reserves():
    frozen = A.decode("ethereum", "UNI", "0x1f98", 18, [18, 0, 7400, 11000, 0, 1, 1, 0, 1, 1],
                      [0, 0, 397_000 * 10 ** 18, 0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 1], 0, 0)
    assert not frozen.can_supply and frozen.supply_full and "congelado" in A.status(frozen)
    iso = A.decode("ethereum", "X", "0x1", 18, [18, 5000, 6000, 11000, 0, 1, 0, 0, 1, 0],
                   [0, 0, 10 ** 18, 0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0], 250_000_000, 0)
    assert iso.isolated and iso.debt_ceiling == 2_500_000 and iso.supply_cap is None
    assert "Modo de Aislamiento" in A.status(iso)


def _fake_rpc(listed: bool):
    def rpc(chain, method, params):
        data = params[0]["data"]
        word = lambda *xs: "0x" + "".join(hex(x)[2:].rjust(64, "0") for x in xs)
        if data.startswith("0x" + A.SEL["data_provider"]):
            return word(0xDA7A)
        if not listed and data.startswith("0x" + A.SEL["data"]):
            raise OSError("{'code': 3, 'message': 'execution reverted'}")
        for name, words in (("data", DATA), ("config", CFG), ("caps", CAPS), ("debt_ceiling", [0]), ("paused", [0])):
            if data.startswith("0x" + A.SEL[name]):
                return word(*words)
        raise AssertionError(data)
    return rpc


def test_reserve_reads_and_caches(monkeypatch):
    calls = []
    rpc = _fake_rpc(True)
    monkeypatch.setattr(P, "_rpc", lambda *a: calls.append(a) or rpc(*a))
    r = A.reserve("base", "USDC")
    assert r.token == "USDC" and r.supply_apr == pytest.approx(0.037767895, rel=1e-6)
    n = len(calls)
    assert A.reserve("base", "USDC") is r and len(calls) == n            # caché
    assert A.reserve("base", "ETH").token == "WETH"                       # ETH nativo → WETH
    assert A.reserve("base", "PAXG") is None                             # sin token verificado en la red


def test_unlisted_and_network_errors(monkeypatch):
    monkeypatch.setattr(P, "_rpc", _fake_rpc(False))
    assert A.reserve("ethereum", "PAXG") is None
    assert A.supply_yield("ethereum", "PAXG") == 0.0                     # no listado: sin alternativa
    A._reserve_cache.clear()
    assert A.supply_yield("ethereum", "USDC") == 0.0
    # Fallo de red: None, nunca una excepción
    def down(*a):
        raise OSError("timeout")
    monkeypatch.setattr(P, "_rpc", down)
    A._reserve_cache.clear()
    A._provider_cache.clear()
    assert A.supply_yield("ethereum", "USDC") is None


# ---------------------------------------------------------------- en el análisis de liquidez

def _rich(fake_world, monkeypatch):
    """Comisiones altas para que haya propuestas sin Aave."""
    monkeypatch.setattr(P, "fee_rates", lambda pool, u0, u1: FeeRates(1e-12, 1e-12, 1e-12))


def test_aave_raises_the_bar(fake_world, monkeypatch):
    _rich(fake_world, monkeypatch)
    monkeypatch.setattr(A, "supply_yield", lambda chain, token: 0.0)
    base = op.analyze("ETH/USDC", 5000)
    assert base.proposals                                               # con Aave al 0 % hay propuesta
    best = base.proposals[0]
    assert best.aave_apy == 0.0
    # Si Aave pagara más que el neto con margen, la misma combinación queda descartada
    high = best.net_safe_apr + 0.01
    monkeypatch.setattr(A, "supply_yield", lambda chain, token: high)
    rep = op.analyze("ETH/USDC", 5000)
    same = next(o for o in rep.opportunities if o.cycle == best.cycle and o.pool.id == best.pool.id)
    assert same.aave_apy == pytest.approx(high)
    assert same.excluded == "rinde menos que depositar los mismos tokens en Aave"
    conds = {c: st for _, st, c, _ in op.checklist(rep, same)[0]}
    assert conds["Rinde más que depositar los mismos tokens en Aave"] == op.FAIL


def test_aave_yield_is_weighted_by_the_deposit(fake_world, monkeypatch):
    _rich(fake_world, monkeypatch)
    monkeypatch.setattr(A, "supply_yield", lambda chain, token: {"WETH": 0.01, "USDC": 0.05}[token])
    rep = op.analyze("ETH/USDC", 5000)
    for o in (o for o in rep.opportunities if o.aave_apy is not None):
        share = o.quote.asset_share
        assert o.aave_apy == pytest.approx(share * 0.01 + (1 - share) * 0.05)
        assert o.aave_yields == {"WETH": 0.01, "USDC": 0.05}


def test_idle_capital_mentions_aave(fake_world, monkeypatch):
    monkeypatch.setattr(A, "supply_yield", lambda chain, token: 0.0385)
    rep = op.analyze("ETH/USDC", 5000)
    rep.proposals = []
    text = op.idle_capital(rep)
    assert "Aave V3" in text and "3.85%" in text
    assert "Alternativa sin riesgo de precio" in "\n".join(op.short_answer(rep))
    assert op.to_json(rep)["capital_parado_aave"]["apy"] == 0.0385


def test_without_aave_data_nothing_changes(fake_world):
    rep = op.analyze("ETH/USDC", 5000)                                   # sin red: Aave no responde
    assert all(o.aave_apy is None for o in rep.opportunities)
    assert not any("Aave" in o.excluded for o in rep.opportunities)
    assert "Aave" not in op.idle_capital(rep)


def test_cli_renders_markets(monkeypatch, capsys):
    r = A.decode("base", "USDC", "0x8335", 6, CFG, DATA, CAPS, 0, 0)
    monkeypatch.setattr(A, "markets", lambda chains=None: ([r], []))
    assert A.main(["--red", "base"]) == 0
    out = capsys.readouterr().out
    assert "| Base | USDC | 3.85% |" in out and "LTV" in out
