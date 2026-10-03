"""Uniswap v4 (identificador y lectura del pool), comisiones realmente pagadas y enlaces."""
import pytest

from etapas import liquidez as lq
from etapas import pools as P
from etapas.descubrir import NATIVE, v4_pool_id
from etapas.keccak import keccak256, selector
from etapas.pools import Pool, PoolState

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
V4 = Pool("ethereum-uniswap4-21c67e77", "ETH/USDC", "Uniswap v4", "ethereum",
          "0x21c67e77068de97969ba93d4aab21826d33ca12bb9f565d8496e8fda8a82ca27", "ETH/USDC",
          0.0005, 10, 18, 6, True)
V3 = Pool("ethereum-uniswap-88e6a0c2", "ETH/USDC", "Uniswap v3", "ethereum",
          "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", "USDC/WETH", 0.0005, 10, 6, 18, False)


def test_keccak_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    assert keccak256(b"a" * 200).hex() == keccak256(b"a" * 200).hex() and len(keccak256(b"a" * 200)) == 32
    assert selector("transfer(address,uint256)") == "a9059cbb"          # selector ERC-20 conocido
    assert selector("getSlot0(bytes32)") == P.SEL_SLOT0
    assert selector("getLiquidity(bytes32)") == P.SEL_LIQUIDITY
    assert selector("getFeeGrowthGlobals(bytes32)") == P.SEL_FEE_GROWTH


def test_v4_pool_id_matches_known_pools():
    """ETH/USDC 0,05 % y 0,30 % en Ethereum (identificadores verificados on-chain: su precio
    coincide con el de mercado y Uniswap los muestra como «ETH/USDC»)."""
    assert v4_pool_id(NATIVE, USDC, 500, 10) == V4.address
    assert v4_pool_id(USDC, NATIVE, 500, 10) == V4.address            # el orden no importa
    assert v4_pool_id(NATIVE, USDC, 3000, 60) == \
        "0xdce6394339af00981949f5f3baf27e3610c76326a700af57e4b3e3ae4977f78d"
    assert v4_pool_id(NATIVE, USDC, 500, 60) != V4.address            # otro spacing, otro pool


def _word(n: int) -> str:
    return (n % (1 << 256)).to_bytes(32, "big").hex()


def test_v4_state_parsing(monkeypatch):
    sqrt_price = int((2680.0 * 10 ** (6 - 18)) ** 0.5 * 2 ** 96)        # USDC por ETH, token0 = ETH
    proto = 125 | (125 << 12)                                           # 0,0125 % por sentido
    answers = {P.SEL_SLOT0: "0x" + _word(sqrt_price) + _word(-197383) + _word(proto) + _word(500),
               P.SEL_LIQUIDITY: "0x" + _word(883783927083299270)}

    def fake_rpc(chain, method, params):
        assert params[0]["to"] == P.STATE_VIEW["ethereum"] and params[0]["data"].endswith(V4.address[2:])
        return answers[params[0]["data"][2:10]]
    monkeypatch.setattr(P, "_rpc", fake_rpc)
    st = P._v4_state(V4)
    assert st.price == pytest.approx(2680.0, rel=1e-6) and st.liquidity == 883783927083299270
    assert st.protocol_cut == pytest.approx(0.000125)       # el proveedor cobra casi toda la comisión
    monkeypatch.setattr(P, "_rpc", lambda *a: "0x" + _word(0) * 4)
    assert P._chain_state(V4).error                           # pool sin crear: error, no excepción


def _fake_archive(growth_old, growth_new, t_old=1_000_000, t_new=1_000_000 + 7 * 86400):
    def archive(chain, method, params, retries=3):
        if method == "eth_blockNumber":
            return hex(2_000_005)
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"timestamp": hex(t_new if n == 2_000_000 else t_old)}
        new = int(params[1], 16) == 2_000_000
        g = growth_new if new else growth_old
        data = params[0]["data"]
        if data.startswith("0x" + P.SEL_FEE_GROWTH):
            return "0x" + _word(g[0]) + _word(g[1])
        return "0x" + _word(g[0] if data == "0xf3058399" else g[1])
    return archive


def test_realized_fees_v4_and_v3(monkeypatch):
    """Dólares por unidad de liquidez y día a partir de los acumuladores de comisiones."""
    q = 2 ** 128
    # v4: token0 = ETH (18 dec), token1 = USDC (6 dec). En 7 días, por unidad de liquidez:
    # 7e-15 ETH·wei... se construye para que salgan 1e-15 $ al día de cada token.
    d_eth = int(7 * 1e-15 / 2500 * 10 ** 18 * q)           # ETH a 2.500 $
    d_usdc = int(7 * 1e-15 * 10 ** 6 * q)
    monkeypatch.setattr(P, "_archive", _fake_archive((10, 20), (10 + d_eth, 20 + d_usdc)))
    assert P.realized_fees(V4, 2500.0, 1.0, 7) == pytest.approx(2e-15, rel=1e-6)
    # v3 con los tokens al revés (token0 = USDC) y acumuladores que dan la vuelta (módulo 2^256)
    P._window_cache.clear()
    top = (1 << 256) - 5
    monkeypatch.setattr(P, "_archive", _fake_archive((top, top), ((d_usdc - 5) % (1 << 256), (d_eth - 5) % (1 << 256))))
    assert P.realized_fees(V3, 1.0, 2500.0, 7) == pytest.approx(2e-15, rel=1e-6)
    # Solana no tiene nodo con histórico; un fallo de red devuelve None, no una excepción
    sol = Pool("x", "SOL/USDC", "Orca", "solana", "abc", "SOL/USDC", 0.0004, 4, 9, 6, True)
    assert P.realized_fees(sol, 100.0, 1.0) is None
    P._window_cache.clear()

    def broken(*a, **k):
        raise OSError("caído")
    monkeypatch.setattr(P, "_archive", broken)
    assert P.realized_fees(V4, 2500.0, 1.0) is None


def test_quote_uses_realized_fees_when_available():
    state = PoolState(price=2684.0, liquidity=10 ** 18, protocol_cut=0.25, volume_24h=5e7, tvl=5e7)
    real = lq.quote_pool(V4, state, 2400, 2900, 2686.0, 1000, fee_per_liquidity=4.5e-15)
    mine = lq.liquidity_for_capital(2684.0, real.low, real.high, 1000) * 10 ** 12
    assert real.fee_source == "real"
    assert real.fee_day == pytest.approx(mine * 4.5e-15 * 10 ** 18 / (10 ** 18 + mine))
    model = lq.quote_pool(V4, state, 2400, 2900, 2686.0, 1000)
    assert model.fee_source == "volumen" and model.fee_day != real.fee_day
    no_volume = PoolState(price=2684.0, liquidity=10 ** 18)
    assert lq.quote_pool(V4, no_volume, 2400, 2900, 2686.0, 1000, fee_per_liquidity=4.5e-15).fee_day > 0
    assert lq.quote_pool(V4, no_volume, 2400, 2900, 2686.0, 1000).fee_day is None


def test_links():
    assert V4.app_url == "https://app.uniswap.org/explore/pools/ethereum/" + V4.address
    assert V3.app_url == "https://app.uniswap.org/explore/pools/ethereum/" + V3.address
    assert V4.url == "https://www.geckoterminal.com/eth/pools/" + V4.address
    assert V4.is_v4 and not V3.is_v4
    assert Pool("x", "SOL/USDC", "Orca", "solana", "abc", "SOL/USDC", 0.0004, 4, 9, 6, True).app_url == ""


def test_market_data_retries_missing_pools(monkeypatch):
    """Si GeckoTerminal devuelve menos pools de los pedidos, los que faltan se piden otra vez."""
    calls = []

    def fake_gt(url, retries=3):
        addrs = url.rsplit("/", 1)[1].split(",")
        calls.append(addrs)
        served = addrs[:1] if len(calls) == 1 else addrs       # la primera vez solo contesta uno
        return {"data": [{"attributes": {"address": a, "volume_usd": {"h24": "5"}, "reserve_in_usd": "7"}}
                         for a in served]}
    monkeypatch.setattr(P, "gt_json", fake_gt)
    data = P._market_data([V3, V4])
    assert data == {V3.address: (5.0, 7.0), V4.address: (5.0, 7.0)}
    assert calls == [[V3.address, V4.address], [V4.address]]


def test_market_data_tolerates_null_values(monkeypatch):
    """Un pool vacío llega con volumen nulo: cuenta como 0 y no impide leer los siguientes."""
    def fake_gt(url, retries=3):
        return {"data": [{"attributes": {"address": V3.address, "volume_usd": {"h24": None}, "reserve_in_usd": None}},
                         {"attributes": {"address": V4.address, "volume_usd": {"h24": "5"}, "reserve_in_usd": "7"}}]}
    monkeypatch.setattr(P, "gt_json", fake_gt)
    assert P._market_data([V3, V4]) == {V3.address: (0.0, 0.0), V4.address: (5.0, 7.0)}
