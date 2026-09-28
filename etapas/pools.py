"""Pools de liquidez concentrada de referencia y su estado actual (precio, liquidez activa,
comisión de protocolo, volumen y TVL). Sin claves ni coste:

- Estado on-chain: nodos RPC públicos de publicnode.com (Ethereum, Base, Arbitrum) y el RPC
  público de Solana.
- Volumen de 24 h y TVL: API pública de GeckoTerminal, por dirección exacta del pool.

Los datos fijos de cada pool (orden de los tokens, decimales, tick spacing y comisión) se
verificaron on-chain en sep-2026 y no cambian nunca en un pool ya creado. `tests/test_pools_red.py`
los vuelve a comprobar contra la cadena (ETAPAS_TEST_RED=1).
"""
import base64
import json
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Pool:
    id: str
    asset: str              # símbolo del activo en la lista (BTC, ETH, SOL)
    dex: str                # "Uniswap v3" | "Orca"
    chain: str              # ethereum | base | arbitrum | solana
    address: str
    pair: str               # como lo muestra el DEX: "WETH/USDC"
    fee: float              # comisión del pool (0.0005 = 0,05 %)
    tick_spacing: int
    dec0: int               # decimales de token0 (Orca: token A)
    dec1: int               # decimales de token1 (Orca: token B)
    base_is_token0: bool    # True si el activo es token0; entonces el precio del pool es estable/activo

    @property
    def label(self) -> str:
        return f"{self.dex} · {self.chain.capitalize()} · {self.pair} {_pct(self.fee)}"

    @property
    def url(self) -> str:
        return f"https://www.geckoterminal.com/{_GT_NETWORK[self.chain]}/pools/{self.address}"


def _pct(fee: float) -> str:
    return f"{fee * 100:.2f} %".replace(".", ",")


_GT_NETWORK = {"ethereum": "eth", "base": "base", "arbitrum": "arbitrum", "solana": "solana"}

# Pools con más liquidez frente a USDC/USDT en cada red (DefiLlama y GeckoTerminal, sep-2026).
POOLS: list[Pool] = [
    Pool("btc-base-cbbtc-usdc-005", "BTC", "Uniswap v3", "base",
         "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef", "cbBTC/USDC", 0.0005, 10, 6, 8, False),
    Pool("btc-arb-wbtc-usdc-005", "BTC", "Uniswap v3", "arbitrum",
         "0x0e4831319a50228b9e450861297ab92dee15b44f", "WBTC/USDC", 0.0005, 10, 8, 6, True),
    Pool("btc-eth-wbtc-usdt-005", "BTC", "Uniswap v3", "ethereum",
         "0x56534741cd8b152df6d48adf7ac51f75169a83b2", "WBTC/USDT", 0.0005, 10, 8, 6, True),
    Pool("eth-base-weth-usdc-005", "ETH", "Uniswap v3", "base",
         "0xd0b53d9277642d899df5c87a3966a349a798f224", "WETH/USDC", 0.0005, 10, 18, 6, True),
    Pool("eth-base-weth-usdc-030", "ETH", "Uniswap v3", "base",
         "0x6c561b446416e1a00e8e93e221854d6ea4171372", "WETH/USDC", 0.003, 60, 18, 6, True),
    Pool("eth-arb-weth-usdc-005", "ETH", "Uniswap v3", "arbitrum",
         "0xc6962004f452be9203591991d15f6b388e09e8d0", "WETH/USDC", 0.0005, 10, 18, 6, True),
    Pool("eth-eth-usdc-weth-005", "ETH", "Uniswap v3", "ethereum",
         "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", "USDC/WETH", 0.0005, 10, 6, 18, False),
    Pool("sol-orca-sol-usdc-004", "SOL", "Orca", "solana",
         "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE", "SOL/USDC", 0.0004, 4, 9, 6, True),
]

NOTES = {
    "SOL": ("En Uniswap, SOL solo tiene un pool WETH/SOL de ~1 M$, sin liquidez suficiente. Se usa "
            "Orca (Solana), que funciona igual: liquidez concentrada con los mismos ticks."),
    "ADA": ("ADA no tiene pools de liquidez concentrada con liquidez relevante: no existe en Uniswap ni "
            "en Orca, y los DEX de Cardano (Minswap) usan liquidez en todo el rango. Los rangos se "
            "muestran solo como referencia de volatilidad."),
}


def pools_for(symbol: str) -> tuple[list[Pool], str]:
    pools = [p for p in POOLS if p.asset == symbol.upper()]
    note = NOTES.get(symbol.upper(), "" if pools else
                     f"No hay pools de referencia configurados para {symbol} (se añaden en etapas/pools.py).")
    return pools, note


@dataclass
class PoolState:
    price: float | None = None            # precio humano (estable por activo)
    liquidity: int | None = None          # liquidez activa en el tick actual (unidades del pool)
    protocol_cut: float = 0.0             # fracción de las comisiones que se queda el protocolo
    volume_24h: float | None = None       # dólares
    tvl: float | None = None
    error: str = ""


RPC = {"ethereum": "https://ethereum-rpc.publicnode.com",
       "base": "https://base-rpc.publicnode.com",
       "arbitrum": "https://arbitrum-one-rpc.publicnode.com",
       "solana": "https://api.mainnet-beta.solana.com"}


def _http_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json",
                                             "Accept": "application/json", "User-Agent": "etapas/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _rpc(chain: str, method: str, params: list):
    out = _http_json(RPC[chain], {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in out:
        raise OSError(str(out["error"])[:120])
    return out["result"]


def _evm_state(pool: Pool) -> PoolState:
    slot0 = _rpc(pool.chain, "eth_call", [{"to": pool.address, "data": "0x3850c7bd"}, "latest"])
    words = [int(slot0[2 + 64 * i: 66 + 64 * i], 16) for i in range(7)]
    liq = int(_rpc(pool.chain, "eth_call", [{"to": pool.address, "data": "0x1a686502"}, "latest"]), 16)
    raw = (words[0] / 2 ** 96) ** 2
    fp = words[5]                          # feeProtocol: 4 bits por token, el protocolo cobra 1/N
    cuts = [1 / n if n else 0.0 for n in (fp % 16, fp >> 4)]
    return PoolState(_human(pool, raw), liq, sum(cuts) / 2)


def _orca_state(pool: Pool) -> PoolState:
    """Cuenta Whirlpool: tick_spacing u16 @41, protocol_fee_rate u16 @47 (puntos básicos de la
    comisión), liquidity u128 @49, sqrt_price u128 Q64.64 @65."""
    info = _rpc("solana", "getAccountInfo", [pool.address, {"encoding": "base64"}])
    d = base64.b64decode(info["value"]["data"][0])
    if struct.unpack_from("<H", d, 41)[0] != pool.tick_spacing:
        raise OSError("la cuenta del pool no coincide con la configuración")
    cut = struct.unpack_from("<H", d, 47)[0] / 10_000
    liq = int.from_bytes(d[49:65], "little")
    raw = (int.from_bytes(d[65:81], "little") / 2 ** 64) ** 2
    return PoolState(_human(pool, raw), liq, cut)


def _human(pool: Pool, raw: float) -> float:
    p = raw / 10 ** (pool.dec1 - pool.dec0)
    return p if pool.base_is_token0 else 1 / p


def _chain_state(pool: Pool) -> PoolState:
    try:
        return _orca_state(pool) if pool.chain == "solana" else _evm_state(pool)
    except (OSError, ValueError, KeyError, TypeError, struct.error) as e:
        return PoolState(error=f"no se pudo leer el pool on-chain ({type(e).__name__})")


def _market_data(pools: list[Pool], wait: float = 12.0) -> dict[str, tuple[float, float]]:
    """{dirección: (volumen 24 h, TVL)} desde GeckoTerminal: una petición por red (su límite
    gratuito es de pocas peticiones por minuto; si responde 429 se espera y se reintenta)."""
    out: dict[str, tuple[float, float]] = {}
    by_net: dict[str, list[str]] = {}
    for p in pools:
        by_net.setdefault(_GT_NETWORK[p.chain], []).append(p.address)
    for net, addrs in by_net.items():
        url = f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/multi/{','.join(addrs)}"
        for attempt in range(3):
            try:
                for item in _http_json(url)["data"]:
                    a = item["attributes"]
                    out[a["address"].lower()] = (float(a["volume_usd"]["h24"]), float(a["reserve_in_usd"]))
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 2:
                    break
                time.sleep(wait)
            except (OSError, ValueError, KeyError, TypeError):
                break
    return out


def fetch_states(pools: list[Pool]) -> dict[str, PoolState]:
    """Estado actual de cada pool, por id. Nunca lanza: los fallos quedan en `error`."""
    states = {p.id: _chain_state(p) for p in pools}
    market = _market_data(pools) if pools else {}
    for p in pools:
        vol = market.get(p.address.lower())
        if vol:
            states[p.id].volume_24h, states[p.id].tvl = vol
        elif not states[p.id].error:
            states[p.id].error = "sin volumen de GeckoTerminal: no se estiman comisiones"
    return states
