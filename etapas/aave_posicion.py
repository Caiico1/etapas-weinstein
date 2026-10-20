"""Posiciones de una dirección en Aave V3 (Ethereum —mercados principal, Lido y EtherFi—, Base y
Arbitrum), leídas de la blockchain. Solo lectura: no pide la cartera ni firma nada.

    python -m etapas.aave_posicion                          # las direcciones de ~/.cripto-renta/config.json
    python -m etapas.aave_posicion 0xD580...eE27 --red base --json

Para cada mercado con saldo: depósitos y deudas (tokens, USD al precio del oráculo de Aave, APY de hoy,
si cuenta como garantía), categoría E-Mode, factor de salud (el de Aave y el recalculado), LTV usado y
la distancia a la liquidación: cuánto tendrían que caer (o subir, si se debe un activo volátil) todos
los activos que no son estables a la vez para que el HF llegue a 1. Rendimiento neto anual de la
posición con los tipos de hoy (depósitos − préstamos, sobre lo neto).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import pools as P
from .aave import SECONDS_PER_YEAR, apy
from .keccak import keccak256

MARKETS = {"ethereum": {"ethereum": "0x2f39d218133afab8f2b819b1066c7e434ad94e9e",
                        "lido": "0xcfbf336fe147d643b9cb705648500e101504b16d",
                        "etherfi": "0xeba440b438ad808101d1c451c1c5322c90befcda"},
           "base": {"base": "0xe20fcbdbffc4dd138ce8b2e6fbb6cb49777ad64d"},
           "arbitrum": {"arbitrum": "0xa97684ead0e402dc232d5a977953df7ecbab3cdb"}}
STABLES = {"USDC", "USDT", "DAI", "USDe", "sUSDe", "USDS", "PYUSD", "FRAX", "GHO", "USDC.e", "USDbC", "USDT0", "USD₮0",
           "RLUSD", "USD1", "LUSD", "crvUSD", "USDtb", "USDG", "EURC", "sDAI", "syrupUSDC", "syrupUSDT", "mUSD"}
CONFIG = Path.home() / ".cripto-renta" / "config.json"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
RAY = 1e27


def _sel(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


SEL = {k: _sel(v) for k, v in {
    "pool": "getPool()", "oracle": "getPriceOracle()", "account": "getUserAccountData(address)",
    "emode": "getUserEMode(address)", "user_config": "getUserConfiguration(address)", "list": "getReservesList()",
    "reserve": "getReserveData(address)", "balance": "balanceOf(address)", "price": "getAssetPrice(address)",
    "symbol": "symbol()", "emode_config": "getEModeCategoryCollateralConfig(uint8)",
    "emode_collateral": "getEModeCategoryCollateralBitmap(uint8)", "emode_label": "getEModeCategoryLabel(uint8)",
    "aggregate3": "aggregate3((address,bool,bytes)[])"}.items()}


# --- Multicall3 (una sola llamada para muchas lecturas) ----------------------------------------

def _w(n: int) -> str:
    return f"{n:064x}"


def _arg(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


def multicall(chain: str, calls: list[tuple[str, str]]) -> list[bytes | None]:
    out: list[bytes | None] = []
    for i in range(0, len(calls), 300):
        part = calls[i:i + 300]
        elems = []
        for target, data in part:
            raw = bytes.fromhex(data)
            elems.append(_w(int(target, 16)) + _w(1) + _w(0x60) + _w(len(raw)) + raw.hex() + "00" * ((-len(raw)) % 32))
        offs, pos = [], 32 * len(elems)
        for e in elems:
            offs.append(_w(pos))
            pos += len(e) // 2
        data = "0x" + SEL["aggregate3"] + _w(0x20) + _w(len(elems)) + "".join(offs) + "".join(elems)
        d = bytes.fromhex(P._rpc(chain, "eth_call", [{"to": MULTICALL3, "data": data}, "latest"])[2:])
        w = lambda off: int.from_bytes(d[off:off + 32], "big")
        base = w(0)
        for k in range(w(base)):
            el = base + 32 + w(base + 32 + 32 * k)
            boff = el + w(el + 32)
            body = d[boff + 32: boff + 32 + w(boff)]
            out.append(body if w(el) and body else None)
    return out


def _u(b: bytes | None, i: int = 0) -> int | None:
    return None if b is None or len(b) < 32 * (i + 1) else int.from_bytes(b[32 * i: 32 * (i + 1)], "big")


def _addr(b: bytes | None, i: int = 0) -> str:
    return f"0x{_u(b, i) or 0:040x}"


def _str(b: bytes | None) -> str | None:
    if not b:
        return None
    if len(b) == 32:
        return b.rstrip(b"\0").decode("utf-8", "replace")
    n = _u(b, 1) or 0
    return b[64:64 + n].decode("utf-8", "replace")


# --- posición ------------------------------------------------------------------------------------

@dataclass
class Holding:
    symbol: str
    supplied: float                # tokens depositados
    supplied_usd: float
    debt: float                    # tokens debidos (tipo variable)
    debt_usd: float
    price_usd: float
    collateral: bool               # el depósito cuenta como garantía
    liq_threshold: float           # el que aplica (el de E-Mode si la categoría lo incluye)
    supply_apy: float
    borrow_apy: float

    @property
    def stable(self) -> bool:
        return self.symbol in STABLES


@dataclass
class Position:
    address: str
    chain: str
    market: str
    emode: int
    emode_label: str
    collateral_usd: float
    debt_usd: float
    health_factor: float           # el que calcula Aave (getUserAccountData)
    health_recalc: float           # recalculado con los depósitos y deudas de abajo
    ltv_used: float                # deuda / garantía
    fall_to_liq: float | None      # caída de todo lo no estable a la vez que lleva el HF a 1
    rise_to_liq: float | None      # subida que lo lleva a 1 (si se debe un activo volátil)
    net_apy: float | None          # (depósitos × APY − deudas × APY) / (depósitos − deudas)
    holdings: list[Holding] = field(default_factory=list)


def _hf(holdings: list[Holding], shock: float = 0.0) -> float:
    """HF con todo lo no estable multiplicado por (1 + shock)."""
    m = lambda h: 1.0 if h.stable else 1 + shock
    coll = sum(h.supplied_usd * m(h) * h.liq_threshold for h in holdings if h.collateral)
    debt = sum(h.debt_usd * m(h) for h in holdings)
    return coll / debt if debt > 0 else float("inf")


def _solve(holdings: list[Holding], lo: float, hi: float) -> float | None:
    """Shock en [lo, hi] con HF = 1 (bisección); None si en ese tramo no se llega."""
    f = lambda s: _hf(holdings, s) - 1
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def position(address: str, chain: str, market: str) -> Position | None:
    provider = MARKETS[chain][market]
    r = multicall(chain, [(provider, SEL["pool"]), (provider, SEL["oracle"])])
    pool, oracle = _addr(r[0]), _addr(r[1])
    r = multicall(chain, [(pool, SEL["account"] + _arg(address)), (pool, SEL["emode"] + _arg(address)),
                          (pool, SEL["user_config"] + _arg(address)), (pool, SEL["list"])])
    acc, emode, ucfg, lst = r[0], _u(r[1]) or 0, _u(r[2]) or 0, r[3]
    coll_base, debt_base = (_u(acc, 0) or 0) / 1e8, (_u(acc, 1) or 0) / 1e8
    if coll_base == 0 and debt_base == 0:
        return None
    assets = [_addr(lst, 2 + i) for i in range(_u(lst, 1) or 0)]
    data = multicall(chain, [(pool, SEL["reserve"] + _arg(a)) for a in assets])
    calls = []
    for a, d in zip(assets, data):
        calls += [(_addr(d, 8), SEL["balance"] + _arg(address)), (_addr(d, 10), SEL["balance"] + _arg(address)),
                  (oracle, SEL["price"] + _arg(a)), (a, SEL["symbol"])]
    if emode:
        calls += [(pool, SEL["emode_config"] + _w(emode)), (pool, SEL["emode_collateral"] + _w(emode)),
                  (pool, SEL["emode_label"] + _w(emode))]
    r = multicall(chain, calls)
    e_lt, e_bitmap, e_label = 0.0, 0, ""
    if emode:
        e_cfg, e_col, e_lab = r[-3:]
        e_lt, e_bitmap, e_label = (_u(e_cfg, 1) or 0) / 1e4, _u(e_col) or 0, _str(e_lab) or ""
    holdings = []
    for k, (a, d) in enumerate(zip(assets, data)):
        sup, deb, price, sym = r[4 * k: 4 * k + 4]
        sup, deb = _u(sup) or 0, _u(deb) or 0
        if not sup and not deb:
            continue
        cfg = _u(d, 0) or 0
        rid = _u(d, 7) or 0
        dec = (cfg >> 48) & 0xFF
        lt = ((cfg >> 16) & 0xFFFF) / 1e4
        if emode and (e_bitmap >> rid) & 1:
            lt = e_lt
        px = (_u(price) or 0) / 1e8
        s_tok, d_tok = sup / 10 ** dec, deb / 10 ** dec
        holdings.append(Holding(_str(sym) or a[:10], s_tok, s_tok * px, d_tok, d_tok * px, px,
                                bool((ucfg >> (2 * rid + 1)) & 1) and lt > 0, lt,
                                apy((_u(d, 2) or 0) / RAY), apy((_u(d, 4) or 0) / RAY)))
    debt_usd = sum(h.debt_usd for h in holdings)
    coll_usd = sum(h.supplied_usd for h in holdings if h.collateral)
    hf_aave = (_u(acc, 5) or 0) / 1e18 if debt_base > 0 else float("inf")
    equity = sum(h.supplied_usd for h in holdings) - debt_usd
    earn = sum(h.supplied_usd * h.supply_apy - h.debt_usd * h.borrow_apy for h in holdings)
    volatile_debt = any(h.debt_usd > 0 and not h.stable for h in holdings)
    return Position(address, chain, market, emode, e_label, coll_usd, debt_usd, hf_aave, _hf(holdings),
                    debt_usd / coll_usd if coll_usd else 0.0,
                    None if debt_usd == 0 else (lambda s: None if s is None else -s)(_solve(holdings, -0.999, 0.0)),
                    _solve(holdings, 0.0, 20.0) if volatile_debt else None,
                    earn / equity if equity > 0 else None, holdings)


def addresses_from_config() -> list[str]:
    try:
        return list(json.loads(CONFIG.read_text(encoding="utf-8")).get("carteras", []))
    except (OSError, ValueError):
        return []


# --- salida -----------------------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}".replace(".", ",")


def _num(x: float, decimals: int = 0) -> str:
    """Formato español: 1.656,90."""
    return f"{x:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _hf_txt(x: float) -> str:
    return "sin deuda" if x == float("inf") else f"{x:.2f}".replace(".", ",")


def render(positions: list[Position], warnings: list[str]) -> str:
    if not positions:
        return "No hay depósitos ni deudas en Aave V3 en esas direcciones y redes." + (
            "\n\nAvisos: " + " · ".join(warnings) if warnings else "")
    out = ["# Posiciones en Aave V3", ""]
    for p in positions:
        out += [f"## {p.chain.capitalize()} · mercado {p.market} · {p.address}", "",
                f"- Garantía {_num(p.collateral_usd)} $ · deuda {_num(p.debt_usd)} $ · LTV usado {_pct(p.ltv_used)}",
                f"- **Factor de salud {_hf_txt(p.health_factor)}** (recalculado {_hf_txt(p.health_recalc)})"
                + (f" · E-Mode {p.emode} «{p.emode_label}»" if p.emode else " · sin E-Mode"),
                f"- Liquidación si todo lo no estable cae un **{_pct(p.fall_to_liq)}**"
                + (f" o sube un **{_pct(p.rise_to_liq)}**" if p.rise_to_liq is not None else ""),
                f"- Rendimiento neto con los tipos de hoy: {_pct(p.net_apy)} anual sobre lo neto", "",
                "| Token | Depositado | $ | Deuda | $ | Garantía | Umbral | APY depósito | APY préstamo |",
                "|---|---|---|---|---|---|---|---|---|"]
        for h in sorted(p.holdings, key=lambda h: -(h.supplied_usd + h.debt_usd)):
            out.append(f"| {h.symbol} | {_num(h.supplied, 4)} | {_num(h.supplied_usd)} | {_num(h.debt, 4)} | {_num(h.debt_usd)} | "
                       f"{'sí' if h.collateral else 'no'} | {_pct(h.liq_threshold)} | {_pct(h.supply_apy)} | "
                       f"{_pct(h.borrow_apy)} |")
        out.append("")
    if warnings:
        out += ["Avisos: " + " · ".join(warnings)]
    out.append("Precios del oráculo de Aave. La distancia a la liquidación mueve a la vez todos los activos no "
               "estables; con varios activos volátiles poco correlacionados es solo orientativa.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.aave_posicion", description=__doc__.split("\n\n")[0])
    ap.add_argument("direcciones", nargs="*", help="direcciones 0x... (por defecto, «carteras» de ~/.cripto-renta/config.json)")
    ap.add_argument("--red", default="", help="ethereum, base, arbitrum (separadas por comas; por defecto, todas)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    addrs = a.direcciones or addresses_from_config()
    if not addrs:
        print("Indica una dirección (0x...) o añádela a «carteras» en ~/.cripto-renta/config.json.", file=sys.stderr)
        return 2
    chains = [c.strip() for c in a.red.split(",") if c.strip()] or list(MARKETS)
    positions, warnings = [], []
    for addr in addrs:
        for chain in chains:
            for market in MARKETS.get(chain, {}):
                try:
                    p = position(addr, chain, market)
                except (OSError, ValueError, KeyError, TypeError) as e:
                    warnings.append(f"{chain}/{market}: no se pudo leer ({type(e).__name__}: {str(e)[:60]})")
                    continue
                if p:
                    positions.append(p)
    if a.json:
        print(json.dumps({"posiciones": [asdict(p) for p in positions], "avisos": warnings}, ensure_ascii=False,
                         indent=1, default=lambda x: None))
    else:
        print(render(positions, warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
