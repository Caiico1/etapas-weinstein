"""Liquidez concentrada por temática («oro tokenizado», «bitcoin», «ether»): qué pool conviene
cuando el usuario dice qué quiere tener, no el par exacto.

    python -m etapas.oportunidad oro --capital 1000
    python -m etapas.oportunidad oro --capital 1000 --json

1. Clasificación rápida de todos los pools verificados de la temática (Uniswap v3 y v4 sin hook):
   TVL y volumen de 24 h (GeckoTerminal, una petición por red) y el APR de comisiones del pool
   entero (comisiones / TVL, anualizado) en tres ventanas: 24 h, 7 y 30 días, leído de la blockchain.
   Es la cifra que enseñan los agregadores, para ver si un APR alto es de un solo día.
2. Análisis completo (oportunidad.analyze) de cada par de la temática con liquidez suficiente,
   que es el que descuenta la pérdida frente a mantener, el gas y el cambio de tokens.
3. Respuesta corta con el mejor resultado de todos los pares.
Los pares entre dos tokens del mismo activo (XAUt/PAXG) solo se clasifican: su precio apenas se
mueve y las etapas de Weinstein no tienen sentido en ellos.
"""
import unicodedata
from dataclasses import dataclass, field

from . import oportunidad as op
from . import pools as P
from .config import DISCLAIMER, TIMEFRAMES
from .data import STABLES, DataError, fetch_candles
from .descubrir import TOKENS, discover
from .pools import Pool

RANK_MIN_TVL = 100_000          # la clasificación enseña los pools desde 100 k$ de TVL
EVM_CHAINS = ("ethereum", "base", "arbitrum")


@dataclass(frozen=True)
class Tema:
    name: str                    # cómo se nombra en la salida
    pairs: tuple[str, ...]       # pares que se analizan con el método completo
    related: tuple[str, ...] = ()   # pares entre dos tokens del mismo activo: solo clasificación
    aliases: tuple[str, ...] = ()


TEMAS = {
    "oro": Tema("oro tokenizado", ("XAUT/USD", "PAXG/USD"), ("XAUT/PAXG",),
                ("oro", "oro tokenizado", "gold", "xau", "tokenized gold")),
    "bitcoin": Tema("bitcoin", ("BTC/USD", "ETH/BTC"), (), ("bitcoin", "btc")),
    "ether": Tema("ether", ("ETH/USD", "ETH/BTC"), (), ("ether", "ethereum", "eth")),
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.strip().lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).replace("-", " ").replace("_", " ").split())


def find_theme(text: str) -> Tema | None:
    """La temática que nombra `text` («oro», «Oro tokenizado», «gold»...) o None si es un par."""
    t = _norm(text)
    return next((tema for tema in TEMAS.values() if t in tema.aliases), None)


@dataclass
class PoolRow:
    pool: Pool
    tvl: float
    volume_24h: float | None
    apr_volume: float | None = None     # volumen 24 h × comisión / TVL, anualizado (lo que dan los agregadores)
    apr_day: float | None = None        # comisiones pagadas de verdad / TVL, anualizado: 24 h
    apr_week: float | None = None       # ... 7 días
    apr_month: float | None = None      # ... 30 días
    related: bool = False               # par entre dos tokens del mismo activo

    @property
    def sort_key(self) -> tuple[int, float]:
        """Primero los pools con dato on-chain (30 días, si no 7), luego los que solo tienen volumen."""
        for rank, x in ((2, self.apr_month), (2, self.apr_week), (1, self.apr_volume)):
            if x is not None:
                return rank, x
        return 0, 0.0


@dataclass
class ThemeReport:
    tema: Tema
    capital: float
    chains: list[str] | None
    rows: list[PoolRow] = field(default_factory=list)
    reports: list[op.Report] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # pares sin pools con liquidez suficiente
    warnings: list[str] = field(default_factory=list)


_usd_cache: dict[str, float] = {}


def _asset(symbol: str) -> str:
    s = symbol.upper()
    return op.ALIASES.get(s, s)


def _usd(symbol: str) -> float:
    """Precio en dólares de un token, con el histórico del activo al que sigue (XAUt → PAXG)."""
    a = _asset(symbol)
    if a in STABLES:
        return 1.0
    if a not in _usd_cache:
        c = fetch_candles(op.PRICE_PROXY.get(a, a), TIMEFRAMES["1d"])
        _usd_cache[a] = float(c.current["close"] if c.current is not None else c.df["close"].iloc[-1])
    return _usd_cache[a]


def chains_with_tokens(tema: Tema) -> list[str]:
    """Redes EVM donde hay tokens verificados de todos los activos de algún par de la temática."""
    out = []
    for chain in EVM_CHAINS:
        reg = TOKENS.get(chain, {})
        for pair in tema.pairs:
            b, q = pair.split("/")
            if b in reg and (q in reg or (q == "USD" and ("USDC" in reg or "USDT" in reg))):
                out.append(chain)
                break
    return out


def rank_pools(tema: Tema, chains: list[str] | None = None) -> tuple[list[PoolRow], list[str]]:
    """Todos los pools verificados de la temática con TVL ≥ RANK_MIN_TVL, con su APR de comisiones."""
    warnings, found, related_ids = [], {}, set()
    for pair in tema.pairs + tema.related:
        b, q = (_asset(x) for x in pair.split("/"))
        pools, warn = discover(b, q)
        warnings += warn
        for p in pools:
            if p.chain in EVM_CHAINS and (not chains or p.chain in chains):
                found[p.id] = p
                if pair in tema.related:
                    related_ids.add(p.id)
    if not found:
        return [], warnings
    states = P.fetch_states(list(found.values()))
    rows = []
    for p in found.values():
        st = states.get(p.id)
        if st is None or st.tvl is None or st.tvl < RANK_MIN_TVL:
            continue
        row = PoolRow(p, st.tvl, st.volume_24h, related=p.id in related_ids)
        if st.volume_24h is not None:
            row.apr_volume = st.volume_24h * p.fee * (1 - st.protocol_cut) / st.tvl * 365
        t0, t1 = p.pair.split("/")
        try:
            rates = P.fee_rates(p, _usd(t0), _usd(t1))
        except DataError:
            rates = None
        if rates and st.liquidity:
            per = st.liquidity / st.tvl * 365      # de dólares por unidad de liquidez a APR del pool
            row.apr_day, row.apr_week, row.apr_month = (None if x is None else x * per
                                                        for x in (rates.day, rates.week, rates.month))
        rows.append(row)
    rows.sort(key=lambda r: r.sort_key, reverse=True)
    return rows, warnings


def analyze_theme(tema: Tema, capital: float = 5000.0, chains: list[str] | None = None) -> ThemeReport:
    rep = ThemeReport(tema, capital, chains)
    rep.rows, rep.warnings = rank_pools(tema, chains)
    for pair in tema.pairs:
        b, q = (_asset(x) for x in pair.split("/"))
        if not any(r.tvl >= op.MIN_TVL for r in rep.rows if not r.related and r.pool.asset == f"{b}/{q}"):
            rep.skipped.append(pair)
            continue
        rep.reports.append(op.analyze(pair, capital, chains))
    return rep


def best_report(rep: ThemeReport) -> op.Report | None:
    """El par cuya mejor combinación da el mejor neto (las propuestas, antes que nada)."""
    ok = [r for r in rep.reports if not r.error and op.headline(r) is not None]
    if not ok:
        return None
    return max(ok, key=lambda r: (bool(r.proposals), op.headline(r).net_apr))


def _m(x: float | None) -> str:
    return "—" if x is None else (f"{x / 1e6:,.2f} M$" if x >= 1e6 else f"{x / 1e3:,.0f} k$")


def render_theme(rep: ThemeReport) -> str:
    t = rep.tema
    out = [f"# Liquidez concentrada · {t.name} · capital {rep.capital:,.0f} $", ""]
    best = best_report(rep)
    out += ["## Respuesta corta", ""]
    if best is None:
        out += [f"- **No hay ningún pool de {t.name} que se pueda evaluar**"
                + (" en " + ", ".join(c.capitalize() for c in rep.chains) if rep.chains else "")
                + ": ninguno tiene liquidez suficiente o faltan datos.", ""]
    else:
        lines = op.short_answer(best)[2:-1]
        lines.insert(0, f"- **De todo el {t.name}, el par con mejor resultado neto es {best.base}/{best.quote}.**")
        top = next((r for r in rep.rows if not r.related), None)
        if top is not None and top.pool.id != op.headline(best).pool.id:
            mine = [o for r in rep.reports for o in r.opportunities if o.pool.id == top.pool.id and o.net_apr is not None]
            why = next((x.split(": ", 1)[1] for r in rep.reports for x in r.illiquid
                        if x.startswith(top.pool.label + ":")), None)
            verdict = (f"su mejor neto es {op._pct(max(o.net_apr for o in mine))} anual" if mine
                       else f"el análisis completo lo descarta ({why})" if why else "no se pudo calcular su neto")
            lines.insert(-1, f"- **El que más comisiones paga** es {top.pool.label} (APR del pool: "
                             f"{op._apr(top.apr_day)} en 24 h, {op._apr(top.apr_week)} en 7 días, "
                             f"{op._apr(top.apr_month)} en 30 días), pero una vez descontada la pérdida frente a "
                             f"mantener {verdict}.")
        out += lines + [""]
    have = chains_with_tokens(t)
    missing = [c for c in EVM_CHAINS if c not in have]
    if missing:
        out += [f"Redes buscadas: {', '.join(c.capitalize() for c in have) or 'ninguna'}. En "
                f"{', '.join(c.capitalize() for c in missing)} no hay tokens de {t.name} verificados en esta "
                f"herramienta, así que no se buscan pools allí.", ""]
    out += [f"## Pools de {t.name}: clasificación rápida", ""]
    if rep.rows:
        out += ["| Pool | TVL | Vol. 24 h | APR 24 h (volumen) | APR 24 h | APR 7 d | APR 30 d |",
                "|---|---|---|---|---|---|---|"]
        for r in rep.rows:
            out.append(f"| {r.pool.label}{' ¹' if r.related else ''} | {_m(r.tvl)} | {_m(r.volume_24h)} | "
                       f"{op._apr(r.apr_volume)} | {op._apr(r.apr_day)} | {op._apr(r.apr_week)} | "
                       f"{op._apr(r.apr_month)} |")
        out += ["", "APR del pool entero = comisiones / TVL, anualizado: es la cifra que dan los agregadores. "
                    "«Volumen» usa el volumen de 24 h × comisión; las otras tres, lo que pagó de verdad el pool "
                    "(blockchain) en cada ventana. Ordenado por el de 30 días. **No descuenta la pérdida frente a "
                    "mantener los tokens**: eso lo hace el análisis completo de cada par, más abajo. Un APR de "
                    "24 h muy por encima del de 30 días es un día de volumen fuerte, no lo normal."]
        if any(r.related for r in rep.rows):
            out += ["", "¹ Par entre dos tokens del mismo activo: su precio apenas se mueve y el método de etapas "
                        "no se le aplica; solo se clasifica."]
    else:
        out.append(f"No hay pools de {t.name} con al menos {_m(RANK_MIN_TVL)} de TVL"
                   + (" en " + ", ".join(c.capitalize() for c in rep.chains) if rep.chains else "") + ".")
    if rep.skipped:
        out += ["", "Sin análisis completo (ningún pool llega a " + _m(op.MIN_TVL) + " de TVL): "
                + ", ".join(rep.skipped) + "."]
    if rep.warnings:
        out += ["", "Avisos: " + " · ".join(rep.warnings)]
    for r in rep.reports:
        out += ["", "---", "", op.render(r, footer=False)]
    out += op.how_it_works() if rep.reports else ["", f"_{DISCLAIMER}_"]
    return "\n".join(out)


def theme_json(rep: ThemeReport) -> dict:
    best = best_report(rep)
    return {"tema": rep.tema.name, "capital": rep.capital,
            "mejor_par": None if best is None else f"{best.base}/{best.quote}",
            "clasificacion": [{"pool": r.pool.label, "red": r.pool.chain, "dex": r.pool.dex,
                               "direccion": r.pool.address, "enlace_dex": r.pool.app_url or None,
                               "tvl": r.tvl, "volumen_24h": r.volume_24h, "apr_24h_volumen": r.apr_volume,
                               "apr_24h": r.apr_day, "apr_7d": r.apr_week, "apr_30d": r.apr_month,
                               "mismo_activo": r.related} for r in rep.rows],
            "redes_con_tokens": chains_with_tokens(rep.tema), "sin_analisis_completo": rep.skipped,
            "avisos": rep.warnings, "pares": [op.to_json(r) for r in rep.reports], "aviso": DISCLAIMER}
