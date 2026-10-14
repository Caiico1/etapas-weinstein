"""Seguimiento de posiciones abiertas: cuentas con una posición falsa (sin red). La lectura on-chain se
comprobó contra la blockchain: las comisiones de v3 coinciden al wei con collect() simulado desde el
dueño, y la liquidez de v4 leída en StateView coincide con la del PositionManager."""
import math

import pytest

from etapas import seguimiento as S
from etapas.liquidez import amounts_per_liquidity

WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"     # Arbitrum, token0 (dirección menor)
USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"     # Arbitrum, token1


def _tick(price):                                        # USDC por ETH → tick (unidades mínimas)
    return round(math.log(price * 1e-12) / math.log(1.0001))


def _reader(price, low=2000, high=3000, liq=10 ** 15, fees=(10 ** 15, 2 * 10 ** 6)):
    def read(chain, token_id):
        return S.OnChain("v3", chain, token_id, WETH, USDC, 0.0005, _tick(low), _tick(high), liq,
                         math.sqrt(price * 1e-12), _tick(price), *fees)
    return read


def test_position_inside_and_fees_in_dollars():
    t = S.track("arbitrum", 1, reader=_reader(2500))
    assert not t.error and t.status == "dentro" and (t.base, t.quote) == ("ETH", "USDC")
    assert t.low == pytest.approx(2000, rel=1e-3) and t.high == pytest.approx(3000, rel=1e-3)
    assert t.fees_base == pytest.approx(0.001) and t.fees_quote == pytest.approx(2.0)
    assert t.fees_usd == pytest.approx(0.001 * 2500 + 2, rel=1e-6)
    x, y = amounts_per_liquidity(2500, t.low, t.high)        # misma fórmula que el resto del proyecto
    assert t.base_amount / t.quote_amount == pytest.approx(x / y, rel=1e-3)


def test_comparisons_against_usdc_and_holding():
    same = S.track("arbitrum", 1, reader=_reader(2500))
    capital = same.value_usd
    t = S.track("arbitrum", 1, capital=capital, entry_price=2500, reader=_reader(2500))
    assert t.vs_usdc == pytest.approx(t.fees_usd, abs=0.01)             # sin moverse el precio
    assert t.vs_hodl == pytest.approx(t.fees_usd, abs=0.01)
    down = S.track("arbitrum", 1, capital=capital, entry_price=2500, reader=_reader(2200))
    assert down.price_effect < 0 and down.hodl_usd > down.value_usd    # pérdida frente a mantener (IL)
    assert down.vs_usdc == pytest.approx(down.value_usd + down.fees_usd - capital)
    assert "no compensan" in S.verdict(down)
    assert "compensan" in S.verdict(t) and "no compensan" not in S.verdict(t)


def test_out_of_range_and_closed():
    out = S.track("arbitrum", 1, capital=100, reader=_reader(1900))
    assert out.status == "fuera por abajo" and out.quote_amount == 0
    assert "fuera de rango" in S.verdict(out)
    closed = S.track("arbitrum", 1, reader=_reader(2500, liq=0))
    assert closed.status.startswith("cerrada")


def test_parse_target_accepts_uniswap_urls():
    assert S.parse_target(["https://app.uniswap.org/positions/v4/base/12345"], False) == ("base", 12345, "v4")
    assert S.parse_target(["https://app.uniswap.org/positions/v3/ethereum/9"], False) == ("ethereum", 9, "v3")
    assert S.parse_target(["Arbitrum", "77"], True) == ("arbitrum", 77, "v4")
    with pytest.raises(ValueError):
        S.parse_target(["base"], False)


def test_unsupported_chain_is_reported():
    t = S.track("solana", 1)
    assert t.error and "No se puede seguir" in S.render(t)
