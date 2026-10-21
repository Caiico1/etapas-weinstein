"""Seguimiento de una posición de liquidez abierta en Uniswap v3 o v4: comisiones cobradas frente a
lo que ha variado la posición.

    python -m etapas.seguimiento base 123456 --capital 4500 --precio 2650
    python -m etapas.seguimiento https://app.uniswap.org/positions/v4/base/12345 --capital 4500

Todo se lee de la blockchain con nodos públicos (sin claves ni cartera):
- v3: NonfungiblePositionManager.positions(id) da el rango, la liquidez y lo ya acumulado; las
  comisiones pendientes salen de feeGrowthInside del pool (whitepaper de Uniswap v3, sección 6.3).
- v4: PositionManager.getPoolAndPositionInfo(id) da el pool y el rango; StateView da la liquidez,
  feeGrowthInside actual y el de la última vez que se tocó la posición.
Con el capital y el precio de entrada se reconstruye lo depositado y se compara con dos referencias:
haber dejado el capital en USDC y haber mantenido los dos tokens sin aportar liquidez.
"""
import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field

from . import pools as P
from .config import DISCLAIMER
from .data import STABLES, DataError, canonical_pair
from .descubrir import NATIVE, TOKENS
from .keccak import keccak256
from .liquidez import composition

Q128 = 2 ** 128
MOD = 2 ** 256

# Uniswap v3: NonfungiblePositionManager y fábrica de cada red (documentación oficial de Uniswap)
V3_MANAGER = {"ethereum": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
              "arbitrum": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
              "base": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"}
# Uniswap v4: PositionManager de cada red (documentación oficial de Uniswap)
V4_MANAGER = {"ethereum": "0xbD216513d74C8cf14cf4747E6AaA6420FF64ee9e",
              "base": "0x7C5f5A4bBd8fD63184577525326123B519429bDc",
              "arbitrum": "0xd88F38F930b7952f2DB2432Cb002E7abbF3dD869"}


def _sel(signature: str) -> str:
    return keccak256(signature.encode()).hex()[:8]


SEL = {name: _sel(sig) for name, sig in {
    "positions": "positions(uint256)",
    "factory": "factory()",
    "getPool": "getPool(address,address,uint24)",
    "slot0": "slot0()",
    "ticks": "ticks(int24)",
    "g0": "feeGrowthGlobal0X128()",
    "g1": "feeGrowthGlobal1X128()",
    "decimals": "decimals()",
    "symbol": "symbol()",
    "poolAndInfo": "getPoolAndPositionInfo(uint256)",
    "posInfo": "getPositionInfo(bytes32,address,int24,int24,bytes32)",
    "inside": "getFeeGrowthInside(bytes32,int24,int24)",
}.items()}


# ---------------------------------------------------------------- lectura on-chain

def _call(chain: str, to: str, data: str) -> list[int]:
    res = P._rpc(chain, "eth_call", [{"to": to, "data": "0x" + data}, "latest"])
    raw = res[2:]
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]


def _word(x: int) -> str:
    return format(x % MOD, "064x")


def _addr(a: str) -> str:
    return a[2:].lower().rjust(64, "0")


def _signed(x: int, bits: int = 256) -> int:
    x &= (1 << bits) - 1
    return x - (1 << bits) if x >= 1 << (bits - 1) else x


def _token(chain: str, address: str) -> tuple[str, str, int]:
    """(activo de la lista, símbolo del token, decimales). Fuera de la lista, lo lee del contrato."""
    if int(address, 16) == 0:
        return "ETH", "ETH", 18
    for asset, toks in TOKENS.get(chain, {}).items():
        for sym, addr, dec in toks:
            if addr.lower() == address.lower():
                return asset, sym, dec
    dec = _call(chain, address, SEL["decimals"])[0]
    raw = P._rpc(chain, "eth_call", [{"to": address, "data": "0x" + SEL["symbol"]}, "latest"])[2:]
    try:
        n = int(raw[64:128], 16)
        sym = bytes.fromhex(raw[128:128 + 2 * n]).decode()
    except (ValueError, UnicodeDecodeError):
        sym = address[:8]
    return sym.upper(), sym, dec


@dataclass
class OnChain:
    version: str
    chain: str
    token_id: int
    token0: str
    token1: str
    fee: float
    tick_lower: int
    tick_upper: int
    liquidity: int
    sqrt_price: float           # sqrtPriceX96 / 2^96 (unidades mínimas)
    tick: int
    fees0: int                  # comisiones pendientes de cobrar, en unidades mínimas
    fees1: int
    pool: str = ""              # dirección del pool (v4: su identificador de 32 bytes)


def _inside(g: int, out_l: int, out_u: int, tick: int, tl: int, tu: int) -> int:
    below = out_l if tick >= tl else (g - out_l) % MOD
    above = out_u if tick < tu else (g - out_u) % MOD
    return (g - below - above) % MOD


def read_v3(chain: str, token_id: int) -> OnChain:
    mgr = V3_MANAGER[chain]
    w = _call(chain, mgr, SEL["positions"] + _word(token_id))
    if len(w) < 12:
        raise OSError("la posición no existe en Uniswap v3")
    t0, t1 = "0x" + format(w[2], "040x"), "0x" + format(w[3], "040x")
    fee, tl, tu, liq = w[4], _signed(w[5]), _signed(w[6]), w[7]
    last0, last1, owed0, owed1 = w[8], w[9], w[10], w[11]
    factory = "0x" + format(_call(chain, mgr, SEL["factory"])[0], "040x")
    pool = "0x" + format(_call(chain, factory, SEL["getPool"] + _addr(t0) + _addr(t1) + _word(fee))[0], "040x")
    s0 = _call(chain, pool, SEL["slot0"])
    sqrt_p, tick = s0[0] / 2 ** 96, _signed(s0[1])
    g0, g1 = _call(chain, pool, SEL["g0"])[0], _call(chain, pool, SEL["g1"])[0]
    lo, up = _call(chain, pool, SEL["ticks"] + _word(tl)), _call(chain, pool, SEL["ticks"] + _word(tu))
    in0 = _inside(g0, lo[2], up[2], tick, tl, tu)
    in1 = _inside(g1, lo[3], up[3], tick, tl, tu)
    fees0 = owed0 + liq * ((in0 - last0) % MOD) // Q128
    fees1 = owed1 + liq * ((in1 - last1) % MOD) // Q128
    return OnChain("v3", chain, token_id, t0, t1, fee / 1e6, tl, tu, liq, sqrt_p, tick, fees0, fees1, pool)


def read_v4(chain: str, token_id: int) -> OnChain:
    mgr, view = V4_MANAGER[chain], P.STATE_VIEW[chain]
    w = _call(chain, mgr, SEL["poolAndInfo"] + _word(token_id))
    if len(w) < 6 or (w[0] == 0 and w[1] == 0):
        raise OSError("la posición no existe en Uniswap v4")
    c0, c1 = "0x" + format(w[0], "040x"), "0x" + format(w[1], "040x")
    fee, spacing, hooks, info = w[2], _signed(w[3]), "0x" + format(w[4], "040x"), w[5]
    tl, tu = _signed(info >> 8, 24), _signed(info >> 32, 24)
    pid = keccak256(b"".join(x.to_bytes(32, "big") for x in (w[0], w[1], fee, spacing % MOD, w[4]))).hex()
    s0 = _call(chain, view, P.SEL_SLOT0 + pid)
    sqrt_p, tick = s0[0] / 2 ** 96, _signed(s0[1])
    pos = _call(chain, view, SEL["posInfo"] + pid + _addr(mgr) + _word(tl) + _word(tu) + _word(token_id))
    liq, last0, last1 = pos[0], pos[1], pos[2]
    ins = _call(chain, view, SEL["inside"] + pid + _word(tl) + _word(tu))
    fees0 = liq * ((ins[0] - last0) % MOD) // Q128
    fees1 = liq * ((ins[1] - last1) % MOD) // Q128
    oc = OnChain("v4", chain, token_id, c0, c1, fee / 1e6, tl, tu, liq, sqrt_p, tick, fees0, fees1, "0x" + pid)
    if int(hooks, 16):
        oc.version = "v4 (con hook)"
    return oc


def amounts(oc: OnChain) -> tuple[float, float]:
    """Tokens de la posición (unidades mínimas) al precio actual (fórmulas de Uniswap v3)."""
    sa, sb, sp, L = 1.0001 ** (oc.tick_lower / 2), 1.0001 ** (oc.tick_upper / 2), oc.sqrt_price, oc.liquidity
    if sp <= sa:
        return L * (1 / sa - 1 / sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (1 / sp - 1 / sb), L * (sp - sa)


# ---------------------------------------------------------------- análisis

@dataclass
class Tracking:
    chain: str
    version: str
    token_id: int
    pair: str = ""
    base: str = ""                         # activo (el precio se expresa en quote por base)
    quote: str = ""
    fee: float = 0.0
    pool: str = ""
    price: float | None = None
    low: float | None = None
    high: float | None = None
    status: str = ""                       # dentro | fuera por abajo | fuera por arriba | cerrada
    base_amount: float = 0.0
    quote_amount: float = 0.0
    value_usd: float | None = None
    fees_usd: float | None = None
    fees_base: float = 0.0
    fees_quote: float = 0.0
    capital: float | None = None
    entry_price: float | None = None
    collected: float = 0.0                 # comisiones ya cobradas antes (las indica el usuario)
    hodl_usd: float | None = None
    vs_usdc: float | None = None           # valor + comisiones − capital
    vs_hodl: float | None = None           # valor + comisiones − mantener
    price_effect: float | None = None      # valor − capital: lo que ha variado la posición sin comisiones
    notes: list[str] = field(default_factory=list)
    error: str = ""
    # Hoy, en dos pasos (oportunidad.decision) para el par en la misma red: ¿se volvería a abrir?
    today: dict | None = None              # {"decision", "paso1", "motivo", "ciclo", "pool"}


def track(chain: str, token_id: int, version: str = "v3", capital: float | None = None,
          entry_price: float | None = None, collected: float = 0.0, reader=None) -> Tracking:
    t = Tracking(chain, version, token_id, capital=capital, entry_price=entry_price, collected=collected)
    managers = V3_MANAGER if version == "v3" else V4_MANAGER
    if chain not in managers:
        t.error = f"Red no admitida para Uniswap {version}: {', '.join(managers)}"
        return t
    try:
        oc = (reader or (read_v3 if version == "v3" else read_v4))(chain, token_id)
        a0, s0, d0 = _token(chain, oc.token0)
        a1, s1, d1 = _token(chain, oc.token1)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        t.error = f"No se pudo leer la posición {token_id} de Uniswap {version} en {chain}: {e}"
        return t
    t.version, t.fee, t.pool = oc.version, oc.fee, oc.pool
    # Orientación: con una estable, el activo primero; entre dos volátiles, como cotiza el par
    if a0 in STABLES and a1 not in STABLES:
        base_is0 = False
    elif a1 in STABLES and a0 not in STABLES:
        base_is0 = True
    else:
        base_is0 = canonical_pair(a0, a1) == (a0, a1)
    (base, bsym, bdec), (quote, qsym, qdec) = (((a0, s0, d0), (a1, s1, d1)) if base_is0
                                                else ((a1, s1, d1), (a0, s0, d0)))
    t.base, t.quote, t.pair = base, quote, f"{bsym}/{qsym}"

    def human(raw_price: float) -> float:          # token1 por token0 (unidades mínimas) → quote por base
        p = raw_price * 10 ** (d0 - d1)
        return p if base_is0 else 1 / p

    prices = sorted(human(1.0001 ** k) for k in (oc.tick_lower, oc.tick_upper))
    t.low, t.high, t.price = prices[0], prices[1], human(oc.sqrt_price ** 2)
    r0, r1 = amounts(oc)
    x0, x1 = r0 / 10 ** d0, r1 / 10 ** d1
    f0, f1 = oc.fees0 / 10 ** d0, oc.fees1 / 10 ** d1
    t.base_amount, t.quote_amount = (x0, x1) if base_is0 else (x1, x0)
    t.fees_base, t.fees_quote = (f0, f1) if base_is0 else (f1, f0)
    if oc.liquidity == 0:
        t.status = "cerrada (sin liquidez)"
    elif t.price < t.low:
        t.status = "fuera por abajo"
    elif t.price > t.high:
        t.status = "fuera por arriba"
    else:
        t.status = "dentro"
    try:
        from .oportunidad import _quote_usd
        q_usd = _quote_usd(quote)
    except DataError as e:
        t.error = f"No hay precio en dólares de {quote}: {e}"
        return t
    t.value_usd = (t.base_amount * t.price + t.quote_amount) * q_usd
    t.fees_usd = (t.fees_base * t.price + t.fees_quote) * q_usd + collected
    if quote not in STABLES:
        t.notes.append(f"Valores en dólares con el precio actual de {quote}.")
    if capital:
        t.price_effect = t.value_usd - capital
        t.vs_usdc = t.value_usd + t.fees_usd - capital
        if entry_price and t.low < entry_price < t.high:
            share = composition(entry_price, t.low, t.high)
            # Lo depositado, con el precio en dólares de quote de hoy (exacto si quote es una estable)
            hb, hq = capital * share / (entry_price * q_usd), capital * (1 - share) / q_usd
            t.hodl_usd = (hb * t.price + hq) * q_usd
            t.vs_hodl = t.value_usd + t.fees_usd - t.hodl_usd
        elif entry_price:
            t.notes.append("El precio de entrada queda fuera del rango: no se reconstruye lo depositado.")
    if oc.version.endswith("(con hook)"):
        t.notes.append("Pool de v4 con hook: el hook puede cambiar comisiones y liquidez; las cifras son orientativas.")
    return t


def add_today(t: Tracking, analyze=None) -> None:
    """Los dos pasos de /pool con los datos de hoy, para el par en la red de la posición: la misma
    lógica para entrar que para seguir, reajustar o salir. Un fallo no impide el seguimiento."""
    if t.error or not t.pair:
        return
    from . import oportunidad as op
    try:
        rep = (analyze or op.analyze)(t.pair, t.capital or 5000.0, [t.chain])
    except Exception as e:      # el análisis de hoy es un añadido
        t.notes.append(f"No se pudo hacer el análisis de hoy ({type(e).__name__}).")
        return
    if rep.error:
        t.notes.append(f"Análisis de hoy: {rep.error}")
        return
    state, o = op.overall(rep)
    t.today = {"decision": state, "paso1": op.step1_line(rep),
               "motivo": op.decision(o, rep.capital)[1] if o else "sin pools con liquidez suficiente",
               "ciclo": o.name.lower() if o else None, "pool": o.pool.label if o else None,
               "rango": [o.quote.low, o.quote.high] if o and o.quote and o.quote.low else None,
               "mismo_pool": bool(o and t.pool and o.pool.address.lower() == t.pool.lower()),
               "gas": o.gas_usd if o else None,
               "stop": [o.stop_low, o.stop_high] if o else None}
    # Lo que se espera de quedarse en el pool de la posición un ciclo más, sin tocarla (por dólar):
    # comisiones + resultado frente a mantener, sin gas ni cambio de tokens. La mejor combinación de ese pool.
    mine = [x for x in rep.opportunities if t.pool and x.pool.address.lower() == t.pool.lower()
            and x.fees_cycle is not None and x.expected_result is not None and x.gas_usd is not None]
    if mine:
        x = max(mine, key=lambda x: (x.fees_cycle + x.expected_result * rep.capital) / x.days)
        t.today["quedarse"] = {"por_dolar": (x.fees_cycle + x.expected_result * rep.capital) / rep.capital,
                               "ciclo": x.name.lower(), "gas": x.gas_usd}


ADD_GAS_SHARE = 0.02      # al añadir, el gas como mucho el 2 % de lo que se añade


def liquidity_move(t: Tracking) -> str:
    """Añadir o quitar liquidez en la misma posición (mismo rango): la misma lógica de dos pasos.
    - Fuera y por debajo del stop-loss (o por encima, entre dos volátiles): quitar toda la liquidez.
    - Dentro, hoy se abriría en este mismo pool y el rango de hoy cae casi entero en el de la
      posición: se puede añadir, con un mínimo para que el gas no pase de ADD_GAS_SHARE.
    - Dentro y hoy no se abriría: quitar si la pérdida esperada en un ciclo más supera el gas de
      retirar; si no, no tocarla (salir costaría más de lo que se espera perder)."""
    d = t.today
    if not d or t.value_usd is None or t.status.startswith("cerrada"):
        return ""
    go = d["decision"] in ("abrir", "abrir con precaución")
    stop = d.get("stop") or [None, None]
    if not go and ((stop[0] and t.price < stop[0]) or (stop[1] and t.price > stop[1])):
        lvl = stop[0] if stop[0] and t.price < stop[0] else stop[1]
        return (f"**Quitar toda la liquidez**: el precio ha cruzado el stop-loss ({_n(lvl, 4)} {t.quote}) "
                f"y hoy no se abriría.")
    if t.status != "dentro":
        return ""
    gas = d.get("gas")
    if go:
        rng = d.get("rango")
        overlap = 0.0
        if rng and t.low and t.high:
            lo, hi = max(rng[0], t.low), min(rng[1], t.high)
            overlap = max(0.0, math.log(hi / lo)) / math.log(rng[1] / rng[0]) if hi > lo else 0.0
        if not d.get("mismo_pool"):
            return (f"**No añadir aquí**: hoy el mejor sitio es otro pool ({d['pool']}). Si quieres poner más "
                    f"capital, ábrelo allí.")
        if overlap < 0.7:
            return (f"**No añadir aquí**: el rango de hoy ({_n(rng[0], 4)} – {_n(rng[1], 4)}) ya no se parece al "
                    f"de la posición; añadir mantendría el rango antiguo. Si quieres más capital, abre una "
                    f"posición nueva con ese rango.")
        share = t.base_amount * t.price / (t.base_amount * t.price + t.quote_amount) if t.value_usd else 0
        txt = (f"**Puedes añadir liquidez** en esta misma posición: hoy se abriría en este pool con un rango "
               f"parecido ({d['decision']}). Se aporta en la proporción actual, {share:.0%} {t.base} y "
               f"{1 - share:.0%} {t.quote}.")
        if gas:
            txt += (f" Añade al menos **{_n(gas / ADD_GAS_SHARE, 0)} $** de una vez: el gas ({_n(gas)} $) sería "
                    f"como mucho el {ADD_GAS_SHARE:.0%} de lo añadido.")
        if d["decision"] == "abrir con precaución":
            txt += " Etapa en precaución: no superes el capital que tenías previsto."
        return txt
    stay = d.get("quedarse")
    if not stay:
        return ""
    expected = stay["por_dolar"] * t.value_usd
    exit_gas = stay["gas"]
    if expected < 0 and -expected > exit_gas:
        return (f"**Quitar liquidez**: hoy no se abriría y quedarse un ciclo {stay['ciclo']} más pierde unos "
                f"{_n(-expected)} $ frente a mantener los tokens, más que el gas de retirarla ({_n(exit_gas)} $). "
                f"Puedes quitar una parte si no quieres salir del todo.")
    if expected < 0:
        return (f"**Ni añadir ni quitar**: hoy no se abriría, pero la pérdida esperada en un ciclo {stay['ciclo']} "
                f"más ({_n(-expected)} $) es menor que el gas de retirarla ({_n(exit_gas)} $).")
    return "**No añadir**: hoy no se abriría; deja la posición como está."


def action(t: Tracking) -> str:
    """Qué hacer con la posición según los dos pasos de hoy y si está dentro o fuera."""
    if t.today is None:
        return ""
    d, inside = t.today, t.status == "dentro"
    go = d["decision"] in ("abrir", "abrir con precaución")
    where = f"ciclo {d['ciclo']} en {d['pool']}" if d["ciclo"] else ""
    if inside and go:
        txt = f"**Mantener**: hoy /pool también la abriría ({d['decision']}, {where})."
    elif inside:
        txt = (f"**Mantener mientras siga dentro, sin reajustar si sale**: hoy /pool no la abriría "
               f"({d['decision']}: {d['motivo']}).")
    elif go:
        rng = d.get("rango")
        txt = (f"**Reajustar**: está fuera y hoy /pool sí abriría ({d['decision']}, {where}"
               + (f", rango {_n(rng[0], 4)} – {_n(rng[1], 4)}" if rng else "") + ").")
    else:
        txt = (f"**No reajustar**: está fuera y hoy /pool no la abriría ({d['decision']}: {d['motivo']}). "
               f"Cierra o quédate con el token que tienes, según tu stop-loss y tu exposición.")
    return f"{txt} Paso 1 (etapa): {d['paso1']}."


def verdict(t: Tracking) -> str:
    if t.vs_usdc is None:
        txt = "Indica --capital (y --precio de entrada) para comparar las comisiones con la variación."
        return txt + (" " + action(t) if t.today else "")
    loss = -min(t.price_effect, 0)
    if t.vs_usdc >= 0:
        txt = ("**Las comisiones compensan** la variación del token: la posición vale más que el capital "
               "que pusiste" + (" aunque el token haya bajado." if loss else "."))
    else:
        txt = (f"**Las comisiones no compensan** todavía la variación: faltan {-t.vs_usdc:,.2f} $ para "
               f"volver al capital inicial.")
    if t.today:
        txt += " " + action(t)
        move = liquidity_move(t)
        if move:
            txt += "\n\n**Añadir o quitar liquidez** (misma posición y rango): " + move
    elif t.status.startswith("fuera"):
        txt += (" La posición está fuera de rango y no cobra comisiones: vuelve a ejecutar /pool para "
                "decidir si reajustar o cerrar (y revisa tu stop-loss).")
    return txt


def _n(x: float | None, d: int = 2) -> str:
    if x is None:
        return "—"
    if x and abs(x) < 1:
        return f"{x:.6g}"
    return f"{x:,.{d}f}"


def render(t: Tracking) -> str:
    if t.error:
        return f"**No se puede seguir la posición**: {t.error}"
    out = [f"# Posición {t.token_id} · Uniswap {t.version} · {t.chain.capitalize()} · {t.pair} "
           f"{t.fee * 100:.2f} %".replace(".", ","), "",
           f"- **Estado**: {t.status}. Precio {_n(t.price, 4)} {t.quote} por {t.base}; rango {_n(t.low, 4)} – "
           f"{_n(t.high, 4)}.",
           f"- **Tokens en la posición**: {_n(t.base_amount, 6)} {t.base} y {_n(t.quote_amount, 4)} {t.quote} "
           f"→ **{_n(t.value_usd)} $**.",
           f"- **Comisiones** pendientes de cobrar: {_n(t.fees_base, 6)} {t.base} y {_n(t.fees_quote, 4)} "
           f"{t.quote}" + (f", más {_n(t.collected)} $ ya cobradas" if t.collected else "")
           + f" → **{_n(t.fees_usd)} $**."]
    if t.capital:
        out += ["", "| Concepto | $ |", "|---|---|",
                f"| Capital inicial | {_n(t.capital)} |",
                f"| Variación de la posición sin comisiones (precio e IL) | {_n(t.price_effect)} |",
                f"| Comisiones | {_n(t.fees_usd)} |",
                f"| **Resultado frente a dejarlo en USDC** | **{_n(t.vs_usdc)}** |"]
        if t.vs_hodl is not None:
            out.append(f"| Resultado frente a mantener los dos tokens (valor de mantener: {_n(t.hodl_usd)}) | "
                       f"{_n(t.vs_hodl)} |")
    out += ["", verdict(t)]
    if t.notes:
        out += ["", "Notas: " + " ".join(t.notes)]
    out += ["", "Las comisiones ya cobradas antes no se ven en la blockchain sin recorrer su historial: si cobraste "
                "alguna, indícala con --cobradas.", "", f"_{DISCLAIMER}_"]
    return "\n".join(out)


def to_json(t: Tracking) -> dict:
    d = {k: v for k, v in t.__dict__.items()}
    d["lectura"] = verdict(t).replace("**", "") if not t.error else None
    d["liquidez"] = liquidity_move(t).replace("**", "") or None if not t.error else None
    return d


_URL = re.compile(r"positions/(v[34])/([a-z]+)/(\d+)", re.I)
_CHAIN_ALIAS = {"mainnet": "ethereum", "eth": "ethereum", "arbitrum_one": "arbitrum", "arbitrum-one": "arbitrum"}


def parse_target(args: list[str], v4: bool) -> tuple[str, int, str]:
    """Admite «red número» o la URL de la posición en app.uniswap.org."""
    m = _URL.search(args[0]) if args else None
    if m:
        return _CHAIN_ALIAS.get(m.group(2).lower(), m.group(2).lower()), int(m.group(3)), m.group(1).lower()
    if len(args) != 2 or not args[1].isdigit():
        raise ValueError("indica la red y el número de la posición (base 123456) o la URL de Uniswap")
    chain = args[0].lower()
    return _CHAIN_ALIAS.get(chain, chain), int(args[1]), "v4" if v4 else "v3"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.seguimiento",
                                 description="Comisiones frente a variación de una posición de Uniswap abierta.")
    ap.add_argument("posicion", nargs="+", help="«red número» (base 123456) o la URL de la posición en Uniswap")
    ap.add_argument("--v4", action="store_true", help="La posición es de Uniswap v4 (por defecto, v3)")
    ap.add_argument("--capital", type=float, help="Dólares que pusiste al abrirla")
    ap.add_argument("--precio", type=float, help="Precio del par al abrirla (en la moneda de cotización)")
    ap.add_argument("--cobradas", type=float, default=0.0, help="Comisiones ya cobradas antes, en dólares")
    ap.add_argument("--json", action="store_true", help="Salida en JSON")
    ap.add_argument("--sin-hoy", action="store_true",
                    help="No hace el análisis de hoy del par (los dos pasos de /pool), que tarda 1-3 minutos")
    args = ap.parse_args(argv)
    try:
        chain, token_id, version = parse_target(args.posicion, args.v4)
    except ValueError as e:
        print(f"**No se puede seguir la posición**: {e}")
        return 1
    t = track(chain, token_id, version, args.capital, args.precio, args.cobradas)
    if not args.sin_hoy:
        add_today(t)
    print(json.dumps(to_json(t), ensure_ascii=False, indent=2, default=float) if args.json else render(t))
    return 1 if t.error else 0


if __name__ == "__main__":
    sys.exit(main())
