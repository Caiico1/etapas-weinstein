"""Cribado de duplas: qué pares merecen una posición de liquidez concentrada hoy, sin que haya que
decir cuáles mirar.

    python -m etapas.cribado --capital 5000
    python -m etapas.cribado --capital 5000 --red base,arbitrum --top 3
    python -m etapas.cribado --solo-filtro          # solo la fase 1 (1-3 minutos)

Fase 1 (barata: una sola petición a DefiLlama Yields, que ya agrega todos los pools de Uniswap v3,
Uniswap v4 y Orca con su TVL, volumen y comisiones). Se quedan los que tienen TVL ≥ 1 M$ y se miden
los criterios de eficiencia:
  - volumen / TVL (criterio: > 1),
  - comisiones / TVL al día (criterio: ≥ 0,1 %), con la media de los últimos 7 días, no con las 24 h
    (un día anómalo o inflado no decide).
(GeckoTerminal no sirve aquí: su límite gratuito real ronda 5-6 peticiones por minuto.)
Solo pasan a la fase 2 los pares cuyos dos tokens están en la lista verificada (descubrir.TOKENS):
un token con el mismo símbolo pero otra dirección nunca entra.

Fase 2 (completa, la de /pool): a los mejores pares de la fase 1 se les aplica oportunidad.analyze:
etapa de Weinstein, rango validado, comisiones reales on-chain, IL, gas, cambio de tokens, margen
de seguridad y stop-loss. Solo se recomiendan los que superan todos los vetos, ordenados por el neto
anual con el margen de seguridad.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import oportunidad as op
from . import pools as P
from .config import DISCLAIMER
from .data import STABLES
from .descubrir import TOKENS

LLAMA_URL = "https://yields.llama.fi/pools"
# Red del proyecto → nombre en DefiLlama
NETWORKS = {"ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum", "solana": "Solana"}
# Proyecto de DefiLlama → DEX (solo protocolos consolidados, los mismos que usa /pool)
PROJECTS = {"uniswap-v3": "Uniswap v3", "uniswap-v4": "Uniswap v4", "orca-dex": "Orca"}
MIN_TVL = op.MIN_TVL
SUSPICIOUS_VOL_TVL = 20.0      # más de 20 veces el TVL en un día suele ser volumen de bots o inflado
YOUNG_POOL_DAYS = 90           # pool con menos historial que esto: sin efecto Lindy
NATIVE_ETH = {"0x0000000000000000000000000000000000000000", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
_FEE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


@dataclass
class Candidate:
    chain: str
    dex: str
    name: str                              # símbolos y comisión, p. ej. «WETH-USDC 0.05%»
    tvl: float
    volume: float                          # volumen medio diario (7 días; si no hay, 24 h)
    week: bool                             # True si volumen y comisiones son de 7 días
    fees_tvl: float | None                 # comisiones diarias / TVL
    days: int | None                       # días de historial del pool en DefiLlama
    base_asset: str | None = None          # símbolo de la lista verificada (None = token sin verificar)
    quote_asset: str | None = None

    @property
    def verified(self) -> bool:
        return self.base_asset is not None and self.quote_asset is not None

    @property
    def stable_pair(self) -> bool:
        return self.verified and self.base_asset in STABLES and self.quote_asset in STABLES

    @property
    def pair(self) -> str:
        """Par para /pool: las estables se agrupan en «USD» (así se miran USDC y USDT a la vez)."""
        b, q = self.base_asset, self.quote_asset
        if b in STABLES:
            b, q = q, b
        return f"{b}/{'USD' if q in STABLES else q}"

    @property
    def vol_tvl(self) -> float:
        return self.volume / self.tvl

    @property
    def hooked(self) -> bool:
        return "hook" in self.name.lower()

    @property
    def eligible(self) -> bool:
        """Puede pasar a la fase 2: tokens verificados, no es un pool entre estables, sin hook, con
        historial suficiente (efecto Lindy) y sin volumen sospechoso."""
        return (self.verified and not self.stable_pair and not self.hooked and self.fees_tvl is not None
                and self.vol_tvl <= SUSPICIOUS_VOL_TVL and (self.days is None or self.days >= YOUNG_POOL_DAYS))

    def flags(self) -> list[str]:
        out = []
        if self.vol_tvl > SUSPICIOUS_VOL_TVL:
            out.append(f"volumen sospechoso ({self.vol_tvl:.0f} × TVL)")
        if self.days is not None and self.days < YOUNG_POOL_DAYS:
            out.append(f"solo {self.days} días de historial")
        if self.hooked:
            out.append("v4 con hook: /pool no lo analiza")
        if self.fees_tvl is None:
            out.append("sin dato de comisiones")
        return out


@dataclass
class Screen:
    capital: float
    chains: list[str]
    min_fees: float
    candidates: list[Candidate] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)          # pares que pasan a la fase 2
    reports: dict = field(default_factory=dict)                  # par → oportunidad.Report
    warnings: list[str] = field(default_factory=list)
    date: str = ""
    deep: bool = True                                            # False: solo la fase 1


# ---------------------------------------------------------------- fase 1

def _asset_of(chain: str, address: str) -> str | None:
    if chain != "solana" and address.lower() in NATIVE_ETH:
        return "ETH"
    for asset, toks in TOKENS.get(chain, {}).items():
        for _, addr, _ in toks:
            if (addr.lower() == address.lower()) if chain != "solana" else addr == address:
                return asset
    return None


def _parse(item: dict, chain: str) -> Candidate | None:
    """Un pool de DefiLlama Yields. Comisiones / TVL diarias: volumen × comisión del pool / TVL si se
    conocen; si no, el APY base (comisiones anualizadas) de DefiLlama / 365."""
    try:
        tvl = float(item.get("tvlUsd") or 0)
        v7, v1 = item.get("volumeUsd7d"), item.get("volumeUsd1d")
        week = v7 is not None
        volume = float(v7) / 7 if week else float(v1 or 0)
        m = _FEE.search(item.get("poolMeta") or "")
        if m and (v7 is not None or v1 is not None):
            fees_tvl = volume * float(m.group(1)) / 100 / tvl
        else:
            apy = item.get("apyBase7d") if item.get("apyBase7d") is not None else item.get("apyBase")
            fees_tvl = None if apy is None else float(apy) / 100 / 365
            week = week and item.get("apyBase7d") is not None
        toks = item.get("underlyingTokens") or []
    except (ValueError, TypeError, ZeroDivisionError):
        return None
    if tvl <= 0:
        return None
    assets = [_asset_of(chain, t) for t in toks[:2]] if len(toks) >= 2 else [None, None]
    name = (item.get("symbol") or "?") + (f" {item['poolMeta']}" if item.get("poolMeta") else "")
    return Candidate(chain, PROJECTS[item["project"]], name, tvl, volume, week, fees_tvl, item.get("count"),
                     *assets)


def fetch_candidates(chains: list[str], warnings: list[str]) -> list[Candidate]:
    try:
        data = P._http_json(LLAMA_URL)["data"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        warnings.append(f"DefiLlama no respondió ({type(e).__name__}): sin fase 1")
        return []
    names = {NETWORKS[c]: c for c in chains}
    out = []
    for item in data:
        if item.get("project") in PROJECTS and item.get("chain") in names and (item.get("tvlUsd") or 0) >= MIN_TVL:
            c = _parse(item, names[item["chain"]])
            if c:
                out.append(c)
    return out


def shortlist(cands: list[Candidate], min_fees: float, top: int) -> list[str]:
    """Pares con algún pool elegible (Candidate.eligible) que cumple el criterio de comisiones / TVL,
    ordenados por su mejor pool."""
    best: dict[str, float] = {}
    for c in cands:
        if c.eligible and c.fees_tvl >= min_fees:
            best[c.pair] = max(best.get(c.pair, 0.0), c.fees_tvl)
    return [p for p, _ in sorted(best.items(), key=lambda kv: -kv[1])][:top]


# ---------------------------------------------------------------- todo junto

def run(capital: float = 5000.0, chains: list[str] | None = None, top: int = 5,
        min_fees: float = op.FEES_TVL_REF, deep: bool = True, progress=None) -> Screen:
    chains = chains or list(NETWORKS)
    s = Screen(capital, chains, min_fees, deep=deep, date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    say = progress or (lambda msg: None)
    say("Fase 1: pools de Uniswap v3, v4 y Orca en " + ", ".join(chains))
    s.candidates = fetch_candidates(chains, s.warnings)
    s.shortlist = shortlist(s.candidates, min_fees, top)
    if deep:
        for pair in s.shortlist:
            say(f"Fase 2: {pair}")
            s.reports[pair] = op.analyze(pair, capital, chains)
    return s


def ranking(s: Screen) -> list[tuple[str, op.Opportunity]]:
    rows = [(pair, o) for pair, rep in s.reports.items() for o in rep.proposals]
    return sorted(rows, key=lambda x: -(x[1].net_safe / s.capital * 365 / x[1].days))


# ---------------------------------------------------------------- salida

def _pct(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:.{d}%}".replace(".", ",")


def _m(x: float) -> str:
    return f"{x / 1e6:,.1f} M$"


def _cand_row(c: Candidate) -> str:
    return (f"| {c.name} | {c.dex} · {c.chain.capitalize()} | {_m(c.tvl)} | {c.vol_tvl:.2f} | "
            f"{_pct(c.fees_tvl, 3)} | {'7 días' if c.week else '24 h'} | "
            f"{'; '.join(c.flags()) or '—'} |")


def render(s: Screen) -> str:
    out = [f"# Cribado de duplas para liquidez concentrada · capital {s.capital:,.0f} $", "",
           f"{s.date} · redes: {', '.join(c.capitalize() for c in s.chains)} · criterio de comisiones: "
           f"≥ {_pct(s.min_fees, 2)} del TVL al día", ""]
    ranked = ranking(s)
    if not s.deep:
        out += ["## Pares que pasan a la fase 2", "",
                (", ".join(f"**{p}**" for p in s.shortlist) if s.shortlist else "Ninguno cumple los criterios.")
                + ". Solo se ha hecho la fase 1: para saber si alguno merece una posición, ejecuta el análisis "
                  "completo (sin --solo-filtro) o /pool con el par.", ""]
    elif ranked:
        out += ["## Duplas recomendadas", "",
                "Cumplen todos los vetos de /pool (etapa, liquidez, neto positivo con margen de seguridad), "
                "ordenadas por el neto anual con el margen de seguridad.", "",
                "| # | Par | Ciclo | Pool | Rango | Neto/ciclo | Neto anual | Con margen | Vol/TVL | Comis./TVL día | Avisos |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, (pair, o) in enumerate(ranked, 1):
            rep = s.reports[pair]
            rows, _ = op.checklist(rep, o)
            warns = sum(1 for _, st, _, _ in rows if st == op.WARN)
            out.append(f"| {i} | {rep.base}/{rep.quote} | {o.name} | {o.pool.label} | {op._p(o.quote.low)} – "
                       f"{op._p(o.quote.high)} | {op._usd(o.net_cycle)} | {op._pct(o.net_apr)} | "
                       f"{op._pct(o.net_safe / s.capital * 365 / o.days)} | "
                       f"{'—' if o.vol_tvl is None else f'{o.vol_tvl:.2f}'} | {_pct(o.fees_tvl, 3)} | "
                       f"{warns} ⚠️ |")
        out += ["", "Para el detalle de cada una (depósito, stop-loss, escenarios y checklist), ejecuta /pool "
                    "con el par y el capital.", ""]
    else:
        out += ["## Ninguna dupla recomendable ahora", ""]
        if not s.shortlist:
            out.append("Ningún par verificado cumple a la vez TVL ≥ 1 M$ y el criterio de comisiones sin volumen "
                       "sospechoso.")
        elif s.reports:
            out.append("Los pares que pasaron el filtro no superan los vetos de /pool (etapa desfavorable o "
                       "comisiones que no compensan la pérdida frente a mantener). Motivos:")
        out += [""] + [f"- **{p}**: {_why(rep)}" for p, rep in s.reports.items()]
        out += ["", "**Mientras tanto**: mantén el capital en USDC en la red donde quieras operar; no asumas la "
                    "volatilidad de un token sin cobrar comisiones por ello.", ""]

    verified = sorted((c for c in s.candidates if c.verified and not c.stable_pair),
                      key=lambda c: -(c.fees_tvl or 0))
    out += ["## Fase 1: pools con tokens verificados", "",
            "| Pool | DEX · red | TVL | Vol/TVL | Comis./TVL día | Datos | Avisos |", "|---|---|---|---|---|---|---|"]
    out += [_cand_row(c) for c in verified] or ["| — | | | | | | |"]
    stables = sorted((c for c in s.candidates if c.stable_pair), key=lambda c: -(c.fees_tvl or 0))[:5]
    if stables:
        out += ["", "**Pools entre estables** (sin riesgo de precio salvo un desanclaje; la etapa de Weinstein no "
                    "aplica, así que no se proponen rangos):", "",
                "| Pool | DEX · red | TVL | Vol/TVL | Comis./TVL día | Datos | Respaldo |", "|---|---|---|---|---|---|---|"]
        for c in stables:
            weak = [t for t in (c.base_asset, c.quote_asset) if t in op.STABLE_BACKING]
            out.append(_cand_row(c).rsplit("|", 2)[0] + "| "
                       + ("⚠️ " + ", ".join(weak) + " sin respaldo 1:1 en efectivo y letras" if weak else "✅")
                       + " |")
    others = sorted((c for c in s.candidates if not c.verified and c.fees_tvl is not None),
                    key=lambda c: -c.fees_tvl)[:10]
    if others:
        out += ["", "**Fuera de la lista verificada** (solo informativo: no se analizan ni se proponen; para "
                    "estudiar uno hay que verificar sus tokens y añadirlos a etapas/descubrir.py, y necesita "
                    "histórico de precios en Binance):", "",
                "| Pool | DEX · red | TVL | Vol/TVL | Comis./TVL día | Datos | Avisos |", "|---|---|---|---|---|---|---|"]
        out += [_cand_row(c) for c in others]
    if s.warnings:
        out += ["", "Avisos: " + " · ".join(s.warnings)]
    out += ["", "**Cómo se criba.** Fase 1 (DefiLlama): todos los pools de Uniswap v3, v4 y Orca con TVL ≥ "
                "1 M$. Para pasar a la fase 2: sus dos tokens verificados, sin hook, al menos "
                f"{YOUNG_POOL_DAYS} días de historial (efecto Lindy), comisiones (volumen medio de 7 días × "
                "comisión del pool) que lleguen al criterio y un volumen que no supere "
                f"{SUSPICIOUS_VOL_TVL:.0f} veces el TVL (suele ser volumen de bots o inflado). Vol/TVL alto no es "
                "bueno por sí solo: a menudo viene de volatilidad, que es lo que genera la pérdida frente a "
                "mantener. Fase 2: el análisis completo de /pool. Son estimaciones con los datos de hoy.",
            "", f"_{DISCLAIMER}_"]
    return "\n".join(out)


def _why(rep: op.Report) -> str:
    if rep.error:
        return rep.error
    best = op.best_per_cycle(rep.opportunities)
    if not best:
        return "sin pools con liquidez suficiente"
    return "; ".join(f"{o.name.lower()}: {o.excluded}" for o in best if o.excluded) or "sin propuesta"


def to_json(s: Screen) -> dict:
    def cand(c: Candidate) -> dict:
        return {"pool": c.name, "red": c.chain, "dex": c.dex, "tvl": c.tvl, "dias_historial": c.days,
                "volumen_dia": c.volume, "base_volumen": "7 días" if c.week else "24 h",
                "volumen_tvl": c.vol_tvl, "comisiones_tvl_dia": c.fees_tvl, "par": c.pair if c.verified else None,
                "verificado": c.verified, "avisos": c.flags()}
    return {"fecha": s.date, "capital": s.capital, "redes": s.chains, "criterio_comisiones": s.min_fees,
            "recomendadas": [dict(op.to_json(s.reports[p])["propuestas"][s.reports[p].proposals.index(o)], par=p)
                             for p, o in ranking(s)],
            "analizados": {p: {"propuestas": len(r.proposals), "motivo": None if r.proposals else _why(r)}
                           for p, r in s.reports.items()},
            "candidatos": [cand(c) for c in s.candidates], "avisos": s.warnings, "aviso": DISCLAIMER}


def load_config(path) -> tuple[float, list[str] | None] | None:
    """cribado.txt (opcional, para la rutina diaria): una línea «CAPITAL [redes separadas por comas]».
    None si no existe o no tiene una línea válida."""
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        try:
            capital = float(parts[0])
        except ValueError:
            return None
        chains = [c.strip().lower() for c in "".join(parts[1:]).split(",") if c.strip()]
        return capital, [c for c in chains if c in NETWORKS] or None
    return None


def save(s: Screen, out_dir) -> None:
    from pathlib import Path
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cribado.md").write_text(render(s), encoding="utf-8")
    (d / "cribado.json").write_text(json.dumps(to_json(s), ensure_ascii=False, indent=2, default=float),
                                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m etapas.cribado",
                                 description="Qué duplas merecen una posición de liquidez concentrada hoy.")
    ap.add_argument("--capital", type=float, default=5000.0, help="Capital por posición en dólares")
    ap.add_argument("--red", default="", metavar="REDES", help="ethereum, base, arbitrum, solana (por defecto, todas)")
    ap.add_argument("--top", type=int, default=5, help="Pares que pasan al análisis completo (por defecto 5)")
    ap.add_argument("--min-comisiones", type=float, default=op.FEES_TVL_REF * 100, metavar="PCT",
                    help="Comisiones / TVL diario mínimas, en %% (por defecto 0,1)")
    ap.add_argument("--solo-filtro", action="store_true", help="Solo la fase 1 (rápida, sin análisis completo)")
    ap.add_argument("--json", action="store_true", help="Salida en JSON")
    ap.add_argument("--guardar", metavar="DIR", help="Guarda también cribado.md y cribado.json en DIR")
    args = ap.parse_args(argv)
    chains = [c.strip().lower() for c in args.red.split(",") if c.strip()] or None
    bad = [c for c in chains or [] if c not in NETWORKS]
    if bad:
        print(f"Redes no admitidas: {', '.join(bad)}. Disponibles: {', '.join(NETWORKS)}")
        return 1
    s = run(args.capital, chains, args.top, args.min_comisiones / 100, not args.solo_filtro,
            progress=lambda m: print(m, file=sys.stderr, flush=True))
    if args.guardar:
        save(s, args.guardar)
    print(json.dumps(to_json(s), ensure_ascii=False, indent=2, default=float) if args.json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
