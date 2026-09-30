"""Descubrimiento de pools de liquidez concentrada para un par, solo entre tokens verificados.

- Uniswap v3 (Ethereum, Base, Arbitrum): se pregunta a la fábrica oficial de cada red
  (getPool(tokenA, tokenB, comisión)) por cada combinación de tokens y comisión. Lo que devuelve la
  fábrica es, por definición, un pool legítimo de Uniswap v3.
- Orca (Solana): GeckoTerminal lista los pools del token; cada candidato se comprueba on-chain
  (la cuenta pertenece al programa Whirlpool y sus dos tokens son los esperados).

Solo se usan los tokens de TOKENS (direcciones verificadas on-chain en sep-2026 por símbolo y
decimales, ver tests/test_pools_red.py): así un token falso con el mismo símbolo nunca entra.
"""
import base64
import struct
import urllib.error

from . import pools as P
from .pools import Pool

# Fábricas de Uniswap v3
FACTORY = {"ethereum": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
           "arbitrum": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
           "base": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"}
FEE_TIERS = [100, 500, 3000, 10000]          # 0,01 %, 0,05 %, 0,30 %, 1 %
WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"

# Tokens verificados: red → símbolo del activo → [(símbolo del token, dirección, decimales)]
TOKENS: dict[str, dict[str, list[tuple[str, str, int]]]] = {
    "ethereum": {
        "ETH": [("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18)],
        "BTC": [("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
                ("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8)],
        "USDC": [("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)],
        "USDT": [("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6)],
        "LINK": [("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA", 18)],
        "UNI": [("UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", 18)],
        "AAVE": [("AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", 18)],
    },
    "base": {
        "ETH": [("WETH", "0x4200000000000000000000000000000000000006", 18)],
        "BTC": [("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8)],
        "USDC": [("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)],
    },
    "arbitrum": {
        "ETH": [("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18)],
        "BTC": [("WBTC", "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f", 8)],
        "USDC": [("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6)],
        "USDT": [("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6)],   # hoy «USD₮0»
        "LINK": [("LINK", "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4", 18)],
        "UNI": [("UNI", "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0", 18)],
        "ARB": [("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548", 18)],
    },
    "solana": {
        "SOL": [("SOL", "So11111111111111111111111111111111111111112", 9)],
        "BTC": [("cbBTC", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", 8)],
        "ETH": [("WETH (Wormhole)", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", 8)],
        "USDC": [("USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6)],
        "USDT": [("USDT", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6)],
    },
}
# «USD» en el par = cualquier estable
QUOTE_ALIASES = {"USD": ["USDC", "USDT"]}


def tokens_for(chain: str, symbol: str) -> list[tuple[str, str, int]]:
    reg = TOKENS.get(chain, {})
    out = []
    for s in QUOTE_ALIASES.get(symbol.upper(), [symbol.upper()]):
        out += reg.get(s, [])
    return out


def supported_symbols() -> list[str]:
    return sorted({s for reg in TOKENS.values() for s in reg} | set(QUOTE_ALIASES))


def _addr_word(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def _uniswap_pools(chain: str, base: str, quote: str) -> list[Pool]:
    out = []
    for bsym, baddr, bdec in tokens_for(chain, base):
        for qsym, qaddr, qdec in tokens_for(chain, quote):
            for fee in FEE_TIERS:
                data = "0x1698ee82" + _addr_word(baddr) + _addr_word(qaddr) + hex(fee)[2:].rjust(64, "0")
                res = P._rpc(chain, "eth_call", [{"to": FACTORY[chain], "data": data}, "latest"])
                address = "0x" + res[-40:]
                if int(address, 16) == 0:
                    continue
                spacing = int(P._rpc(chain, "eth_call", [{"to": address, "data": "0xd0c93a7c"}, "latest"]), 16)
                base0 = int(baddr, 16) < int(qaddr, 16)          # token0 es la dirección menor
                (s0, d0), (s1, d1) = ((bsym, bdec), (qsym, qdec)) if base0 else ((qsym, qdec), (bsym, bdec))
                out.append(Pool(f"{chain}-uniswap-{address[2:10]}", f"{base}/{quote}", "Uniswap v3", chain,
                                address.lower(), f"{s0}/{s1}", fee / 1e6, spacing, d0, d1, base0))
    return out


def _orca_pools(base: str, quote: str, pages: int = 3) -> list[Pool]:
    btoks, qtoks = tokens_for("solana", base), tokens_for("solana", quote)
    if not btoks or not qtoks:
        return []
    qmints = {m: (s, d) for s, m, d in qtoks}
    out, seen = [], set()
    for bsym, bmint, bdec in btoks:
        for page in range(1, pages + 1):
            url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{bmint}/pools?page={page}"
            items = P.gt_json(url).get("data") or []
            for it in items:
                rel = it["relationships"]
                if rel["dex"]["data"]["id"] != "orca":
                    continue
                mints = {rel["base_token"]["data"]["id"].split("_", 1)[1],
                         rel["quote_token"]["data"]["id"].split("_", 1)[1]}
                other = (mints - {bmint}).pop() if bmint in mints and len(mints) == 2 else None
                address = it["attributes"]["address"]
                if other not in qmints or address in seen:
                    continue
                seen.add(address)
                pool = _verify_orca(address, bmint, other, bsym, bdec, *qmints[other], f"{base}/{quote}")
                if pool:
                    out.append(pool)
            if len(items) < 20:
                break
    return out


def _verify_orca(address, bmint, qmint, bsym, bdec, qsym, qdec, pair) -> Pool | None:
    """Comprueba on-chain que es un Whirlpool con esos dos tokens y lee spacing y comisión."""
    info = P._rpc("solana", "getAccountInfo", [address, {"encoding": "base64"}])["value"]
    if not info or info["owner"] != WHIRLPOOL_PROGRAM:
        return None
    d = base64.b64decode(info["data"][0])
    mint_a, mint_b = P.b58encode(d[101:133]), P.b58encode(d[181:213])
    if {mint_a, mint_b} != {bmint, qmint}:
        return None
    spacing = struct.unpack_from("<H", d, 41)[0]
    fee = struct.unpack_from("<H", d, 45)[0] / 1e6
    base0 = mint_a == bmint
    (s0, d0), (s1, d1) = ((bsym, bdec), (qsym, qdec)) if base0 else ((qsym, qdec), (bsym, bdec))
    return Pool(f"solana-orca-{address[:8]}", pair, "Orca", "solana", address, f"{s0}/{s1}", fee,
                spacing, d0, d1, base0)


def discover(base: str, quote: str) -> tuple[list[Pool], list[str]]:
    """Pools de Uniswap v3 y Orca para base/quote. Devuelve (pools, avisos)."""
    pools, warnings = [], []
    for chain in ("ethereum", "base", "arbitrum"):
        try:
            pools += _uniswap_pools(chain, base, quote)
        except (OSError, ValueError, KeyError, TypeError) as e:
            warnings.append(f"Uniswap v3 {chain}: no se pudo consultar ({type(e).__name__})")
    try:
        pools += _orca_pools(base, quote)
    except (OSError, ValueError, KeyError, TypeError, struct.error, urllib.error.URLError) as e:
        warnings.append(f"Orca: no se pudo consultar ({type(e).__name__})")
    return pools, warnings
