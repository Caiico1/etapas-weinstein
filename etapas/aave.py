"""Mercados de Aave V3 leídos de la blockchain (Ethereum, Base y Arbitrum), sin claves ni cartera.

    python -m etapas.aave                      # todas las redes, tokens verificados
    python -m etapas.aave --red base --json

Para cada token verificado (descubrir.TOKENS) se lee del contrato PoolDataProvider de Aave V3 (su
dirección se pide al PoolAddressesProvider de cada red, que no cambia con las actualizaciones):
  - rendimiento por depositar (supply) y coste de pedir prestado (borrow, tipo variable), como APR
    en «ray» que se compone por segundo para dar el APY que enseña la aplicación de Aave;
  - uso del mercado (deuda / depositado), límites de depósito y de deuda (supply/borrow caps);
  - LTV (poder de préstamo de una garantía), umbral de liquidación (margen de mantenimiento) y bonus
    de liquidación; techo de deuda (> 0 = activo en Modo de Aislamiento); activo, congelado, pausado.
Conceptos del Aave V3 Technical Paper (Frangella y Herskind, 2022): LTV y umbral de liquidación
separados (secc. 4.3), Modo de Aislamiento con techo de deuda en USD (4.2), E-Mode (4.1), caps (3.2).

Uso en la liquidez concentrada (oportunidad.py): depositar en Aave los mismos tokens que pide la
posición es la alternativa sin pérdida frente a mantener; la posición solo compensa si su neto
supera ese rendimiento. Y con el capital parado, USDC depositado en Aave en lugar de quieto.
"""
import argparse
import json
import sys
from dataclasses import asdict, dataclass

from . import pools as P
from .config import DISCLAIMER
from .descubrir import TOKENS
from .keccak import keccak256

# PoolAddressesProvider de Aave V3 (mercado principal de cada red). Comprobados on-chain en
# oct-2026: getPool() devuelve los Pool oficiales (0x8787…4E2 Ethereum, 0x794a…4AD Arbitrum,
# 0xA238…1C5 Base).
ADDRESSES_PROVIDER = {"ethereum": "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e",
                      "arbitrum": "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb",
                      "base": "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D"}
CHAINS = tuple(ADDRESSES_PROVIDER)
SECONDS_PER_YEAR = 365 * 86400
RAY = 10 ** 27
NATIVE_ALIAS = {"ETH": "WETH"}            # en Aave se deposita WETH (la aplicación envuelve el ETH)


def _sel(signature: str) -> str:
    return keccak256(signature.encode()).hex()[:8]


SEL = {name: _sel(sig) for name, sig in {
    "data_provider": "getPoolDataProvider()",
    "config": "getReserveConfigurationData(address)",
    "data": "getReserveData(address)",
    "caps": "getReserveCaps(address)",
    "debt_ceiling": "getDebtCeiling(address)",
    "paused": "getPaused(address)",
}.items()}


@dataclass
class Reserve:
    chain: str
    token: str                    # como en descubrir.TOKENS: USDC, WETH, cbBTC...
    address: str
    supply_apr: float             # fracción anual (0.0378 = 3,78 %)
    supply_apy: float
    borrow_apr: float
    borrow_apy: float
    utilization: float            # deuda / depositado
    supplied: float               # tokens depositados
    borrowed: float               # tokens prestados
    ltv: float                    # poder de préstamo de la garantía (0.75 = 75 %)
    liquidation_threshold: float  # margen de mantenimiento
    liquidation_bonus: float      # descuento del liquidador (0.045 = 4,5 %)
    supply_cap: float | None      # tokens; None = sin límite
    borrow_cap: float | None
    debt_ceiling: float           # dólares; > 0 = activo en Modo de Aislamiento
    collateral: bool              # se puede usar como garantía
    borrowing: bool               # se puede pedir prestado
    active: bool
    frozen: bool
    paused: bool

    @property
    def isolated(self) -> bool:
        return self.debt_ceiling > 0

    @property
    def supply_full(self) -> bool:
        """El límite de depósito está alcanzado: no admite más depósitos."""
        return self.supply_cap is not None and self.supplied >= self.supply_cap

    @property
    def can_supply(self) -> bool:
        return self.active and not self.frozen and not self.paused and not self.supply_full


def apy(apr: float) -> float:
    """APY con composición por segundo, como el de la aplicación de Aave."""
    return (1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1


def _words(chain: str, to: str, sel: str, arg: str | None = None) -> list[int]:
    data = "0x" + sel + (arg[2:].lower().rjust(64, "0") if arg else "")
    res = P._rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"])
    return [int(res[2 + 64 * i:66 + 64 * i], 16) for i in range((len(res) - 2) // 64)]


_provider_cache: dict[str, str] = {}


def data_provider(chain: str) -> str:
    """Dirección actual del PoolDataProvider de Aave V3 en la red."""
    if chain not in _provider_cache:
        _provider_cache[chain] = "0x" + hex(_words(chain, ADDRESSES_PROVIDER[chain], SEL["data_provider"])[0])[2:].rjust(40, "0")
    return _provider_cache[chain]


def decode(chain: str, token: str, address: str, decimals: int, cfg: list[int], data: list[int],
           caps: list[int], debt_ceiling: int, paused: int) -> Reserve:
    """Reserve a partir de las palabras que devuelve PoolDataProvider:
    cfg  = (decimales, ltv, umbral, bonus, factor de reserva, garantía, préstamo, tipo estable, activo, congelado)
    data = (sin respaldo, a tesorería, total aToken, deuda estable, deuda variable, tasa de depósito,
            tasa variable, ...) con las tasas en ray (1e27 = 100 % anual)."""
    unit = 10 ** decimals
    supplied, debt = data[2] / unit, (data[3] + data[4]) / unit
    s_apr, b_apr = data[5] / RAY, data[6] / RAY
    return Reserve(chain, token, address, s_apr, apy(s_apr), b_apr, apy(b_apr),
                   debt / supplied if supplied else 0.0, supplied, debt,
                   cfg[1] / 1e4, cfg[2] / 1e4, max(cfg[3] / 1e4 - 1, 0.0),
                   float(caps[1]) or None, float(caps[0]) or None, debt_ceiling / 100,
                   bool(cfg[5]), bool(cfg[6]), bool(cfg[8]), bool(cfg[9]), bool(paused))


_reserve_cache: dict[tuple[str, str], Reserve | None] = {}


def reserve(chain: str, token: str) -> Reserve | None:
    """Mercado de Aave V3 de un token verificado (por su nombre en TOKENS: USDC, WETH, cbBTC; ETH se
    lee como WETH). None si el token no está listado en esa red. Lanza OSError si falla la red."""
    token = NATIVE_ALIAS.get(token, token)
    key = (chain, token)
    if key not in _reserve_cache:
        found = next(((a, d) for toks in TOKENS.get(chain, {}).values() for t, a, d in toks if t == token), None)
        if chain not in ADDRESSES_PROVIDER or found is None:
            _reserve_cache[key] = None
            return None
        address, decimals = found
        dp = data_provider(chain)
        try:
            data = _words(chain, dp, SEL["data"], address)
        except OSError as e:
            if "revert" in str(e):                 # el PoolDataProvider revierte con activos no listados
                _reserve_cache[key] = None
                return None
            raise
        cfg = _words(chain, dp, SEL["config"], address)
        caps = _words(chain, dp, SEL["caps"], address)
        ceiling = _words(chain, dp, SEL["debt_ceiling"], address)[0]
        paused = _words(chain, dp, SEL["paused"], address)[0]
        _reserve_cache[key] = decode(chain, token, address, decimals, cfg, data, caps, ceiling, paused)
    return _reserve_cache[key]


def supply_yield(chain: str, token: str) -> float | None:
    """APY por depositar el token en Aave V3 en esa red: 0 si no está listado o no admite depósitos
    (no hay alternativa en Aave), None si no se pudo leer."""
    try:
        r = reserve(chain, token)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return r.supply_apy if r is not None and r.can_supply else 0.0


def markets(chains=None) -> tuple[list[Reserve], list[str]]:
    """Todos los tokens verificados con mercado en Aave V3, por red."""
    out, warnings = [], []
    for chain in chains or CHAINS:
        for toks in TOKENS.get(chain, {}).values():
            for token, _, _ in toks:
                try:
                    r = reserve(chain, token)
                except (OSError, ValueError, KeyError, TypeError) as e:
                    warnings.append(f"Aave {chain} {token}: no se pudo leer ({type(e).__name__})")
                    continue
                if r is not None:
                    out.append(r)
    return out, warnings


# ---------------------------------------------------------------- salida


def _pct(x: float) -> str:
    return f"{x:.2%}"


def _amount(x: float | None) -> str:
    if x is None:
        return "sin límite"
    return f"{x / 1e6:,.2f} M" if x >= 1e6 else f"{x / 1e3:,.1f} k" if x >= 1e3 else f"{x:,.2f}"


def status(r: Reserve) -> str:
    notes = []
    if r.paused:
        notes.append("pausado")
    if r.frozen:
        notes.append("congelado: no admite depósitos ni préstamos nuevos")
    if r.supply_full:
        notes.append("límite de depósito alcanzado")
    if r.isolated:
        notes.append(f"Modo de Aislamiento (techo de deuda {r.debt_ceiling:,.0f} $)")
    if r.ltv == 0 and r.liquidation_threshold > 0:
        notes.append("LTV 0: no da poder de préstamo nuevo")
    elif not r.collateral:
        notes.append("no sirve de garantía")
    return "; ".join(notes) or "normal"


def render(reserves: list[Reserve], warnings: list[str]) -> str:
    out = ["# Aave V3 · mercados de los tokens verificados", "",
           "| Red | Token | Depositar (APY) | Pedir prestado (APY) | Uso | Depositado | Límite depósito | "
           "LTV | Umbral liquidación | Bonus liquidación | Estado |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(reserves, key=lambda r: (r.chain, -r.supply_apy)):
        out.append(f"| {r.chain.capitalize()} | {r.token} | {_pct(r.supply_apy)} | {_pct(r.borrow_apy)} | "
                   f"{r.utilization:.1%} | {_amount(r.supplied)} | {_amount(r.supply_cap)} | {r.ltv:.0%} | "
                   f"{r.liquidation_threshold:.0%} | {r.liquidation_bonus:.1%} | {status(r)} |")
    out += ["", "APY de tipo variable: cambia con el uso del mercado (a más uso, más paga el depósito y más "
                "cuesta el préstamo). LTV = cuánto puedes pedir prestado por cada dólar de garantía; umbral de "
                "liquidación = a partir de qué deuda te liquidan (factor de salud < 1); el bonus es el descuento "
                "con que el liquidador se queda tu garantía. Riesgos de depositar: contrato inteligente, oráculos, "
                "respaldo de la estable y, en redes L2, caídas del secuenciador (Aave pausa las liquidaciones un "
                "tiempo de gracia, pero también los préstamos)."]
    if warnings:
        out += ["", "Avisos: " + " · ".join(warnings)]
    out += ["", f"_{DISCLAIMER}_"]
    return "\n".join(out)


def to_json(reserves: list[Reserve], warnings: list[str]) -> dict:
    return {"mercados": [dict(asdict(r), aislado=r.isolated, admite_depositos=r.can_supply, estado=status(r))
                         for r in reserves], "avisos": warnings, "aviso": DISCLAIMER}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.aave",
                                 description="Mercados de Aave V3 (depósito, préstamo y riesgo) en la blockchain.")
    ap.add_argument("--red", default="", metavar="REDES", help="ethereum, base, arbitrum (separadas por comas)")
    ap.add_argument("--json", action="store_true", help="Salida en JSON")
    args = ap.parse_args(argv)
    chains = [c.strip().lower() for c in args.red.split(",") if c.strip()] or None
    reserves, warnings = markets(chains)
    print(json.dumps(to_json(reserves, warnings), ensure_ascii=False, indent=2) if args.json
          else render(reserves, warnings))
    return 0 if reserves else 1


if __name__ == "__main__":
    sys.exit(main())
