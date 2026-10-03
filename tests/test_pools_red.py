"""Comprueba contra la cadena los datos fijos de cada pool (orden de tokens, decimales, tick spacing
y comisión) y que su precio cuadra con el de Binance. Usa la red: solo con ETAPAS_TEST_RED=1.

    ETAPAS_TEST_RED=1 python -m pytest tests/test_pools_red.py
"""
import base64
import json
import os
import struct
import time
import urllib.request

import pytest

from etapas import pools as pools_mod
from etapas.pools import POOLS

pytestmark = pytest.mark.skipif(os.environ.get("ETAPAS_TEST_RED") != "1",
                                reason="usa la red (ETAPAS_TEST_RED=1 para activarlo)")

USD = {"ethereum": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdac17f958d2ee523a2206206994597c13d831ec7"},
       "base": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"},
       "arbitrum": {"0xaf88d065e77c8cc2239327c5edb3a432268e5831"},
       "solana": {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "etapas-test"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


@pytest.fixture(autouse=True)
def _con_red(monkeypatch):
    # conftest desconecta la red de los pools: aquí se restaura la función original
    monkeypatch.setattr(pools_mod, "_http_json", _REAL_HTTP)
    monkeypatch.setattr(pools_mod.time, "sleep", _REAL_SLEEP)     # respeta los límites de las APIs


_REAL_HTTP = pools_mod._http_json
_REAL_SLEEP = time.sleep


def _eth_call(chain, to, data):
    return pools_mod._rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"])


@pytest.mark.parametrize("pool", [p for p in POOLS if p.chain != "solana"], ids=lambda p: p.id)
def test_evm_pool_static_data(pool):
    t0 = "0x" + _eth_call(pool.chain, pool.address, "0x0dfe1681")[-40:]
    t1 = "0x" + _eth_call(pool.chain, pool.address, "0xd21220a7")[-40:]
    assert int(_eth_call(pool.chain, t0, "0x313ce567"), 16) == pool.dec0
    assert int(_eth_call(pool.chain, t1, "0x313ce567"), 16) == pool.dec1
    assert int(_eth_call(pool.chain, pool.address, "0xd0c93a7c"), 16) == pool.tick_spacing
    assert int(_eth_call(pool.chain, pool.address, "0xddca3f43"), 16) == round(pool.fee * 1e6)
    quote = t1 if pool.base_is_token0 else t0
    assert quote.lower() in USD[pool.chain]


def test_orca_pool_static_data():
    pool = next(p for p in POOLS if p.chain == "solana")
    info = pools_mod._rpc("solana", "getAccountInfo", [pool.address, {"encoding": "base64"}])
    assert info["value"]["owner"] == "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
    d = base64.b64decode(info["value"]["data"][0])
    assert struct.unpack_from("<H", d, 41)[0] == pool.tick_spacing
    assert struct.unpack_from("<H", d, 45)[0] == round(pool.fee * 1e6)     # fee_rate en millonésimas


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL"])
def test_pool_prices_match_binance(asset):
    ref = float(_get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={asset}USDT")["price"])
    pools = [p for p in POOLS if p.asset == asset]
    states = pools_mod.fetch_states(pools)
    for p in pools:
        st = states[p.id]
        assert st.price is not None, (p.id, st.error)
        assert abs(st.price / ref - 1) < 0.01, (p.id, st.price, ref)
        assert st.liquidity and st.liquidity > 0
        assert 0 <= st.protocol_cut < 0.5


def _decimals_evm(chain, addr):
    return int(_eth_call(chain, addr, "0x313ce567"), 16)


def test_token_registry_on_chain():
    """Cada token del registro existe en su red con esos decimales (Solana: cuenta de mint SPL)."""
    from etapas.descubrir import TOKENS
    for chain, reg in TOKENS.items():
        for toks in reg.values():
            for sym, addr, dec in toks:
                if chain == "solana":
                    info = pools_mod._rpc("solana", "getAccountInfo", [addr, {"encoding": "base64"}])["value"]
                    assert base64.b64decode(info["data"][0])[44] == dec, (chain, sym)
                else:
                    assert _decimals_evm(chain, addr) == dec, (chain, sym)


def test_discovery_finds_known_pools():
    from etapas.descubrir import discover
    found, warnings = discover("ETH", "USDC")
    assert not warnings
    addrs = {p.address.lower() for p in found}
    assert "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640" in addrs      # Ethereum 0,05 %
    assert "0xd0b53d9277642d899df5c87a3966a349a798f224" in addrs      # Base 0,05 %
    sol, _ = discover("SOL", "USDC")
    assert "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE" in {p.address for p in sol}


def test_v4_pools_on_chain():
    """Los pools de Uniswap v4 sin hook se encuentran por su identificador y su precio cuadra con
    Binance (lo que confirma identificador, StateView y decimales)."""
    from etapas.descubrir import discover
    found, warnings = discover("ETH", "USDC")
    assert not warnings
    v4 = [p for p in found if p.is_v4]
    assert "0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27" in {p.address for p in v4}
    assert {p.chain for p in v4} == {"ethereum", "base", "arbitrum"}
    ref = float(_get("https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDT")["price"])
    states = pools_mod.fetch_states(v4)
    big = [p for p in v4 if (states[p.id].tvl or 0) > 1e6]
    assert len(big) >= 3
    for p in big:
        st = states[p.id]
        assert abs(st.price / ref - 1) < 0.01, (p.label, st.price, ref)
        assert st.liquidity > 0 and 0 <= st.protocol_cut < 0.01


def test_realized_fees_on_chain():
    """Lo cobrado por unidad de liquidez es parecido en los pools grandes de ETH/USDC (v3 y v4):
    si el dato on-chain o la conversión a dólares fallaran, no coincidirían."""
    from etapas.descubrir import discover
    found, _ = discover("ETH", "USDC")
    states = pools_mod.fetch_states(found)
    values = {}
    for p in found:
        st = states[p.id]
        if p.chain == "solana" or st.price is None or (st.tvl or 0) < 2e7:
            continue
        usd0, usd1 = (st.price, 1.0) if p.base_is_token0 else (1.0, st.price)
        r = pools_mod.realized_fees(p, usd0, usd1, 7)
        if r is not None:
            values[p.label] = r
    assert len(values) >= 3, values
    lo, hi = min(values.values()), max(values.values())
    assert lo > 0 and hi / lo < 2.0, values
