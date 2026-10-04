"""Vigilancia de oportunidades (oportunidades.txt): cada día se analiza cada par vigilado y se avisa
cuando aparece (o desaparece) una propuesta de liquidez que cumple todos los criterios."""
import re
from dataclasses import dataclass
from pathlib import Path

from . import oportunidad as op

_LINE = re.compile(r"^(\S+)\s+([0-9][0-9.,]*)\s*(.*)$")
CHAINS = {"ethereum", "base", "arbitrum", "solana"}


@dataclass
class Watch:
    pair: str
    capital: float
    chains: tuple[str, ...] = ()        # vacío = todas las redes

    @property
    def key(self) -> str:
        where = "+".join(self.chains) if self.chains else "todas"
        return f"{self.pair} {self.capital:g} {where}"


def load_watches(path: Path) -> tuple[list[Watch], list[str]]:
    """Una vigilancia por línea: «PAR CAPITAL [redes]», por ejemplo «BTC/USDC 5000 base».
    Ignora comentarios (#) y líneas vacías."""
    if not path.exists():
        return [], []
    out, warnings = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        m = _LINE.match(raw)
        try:
            if not m:
                raise ValueError("formato")
            base, quote, _ = op.parse_pair(m.group(1))
            capital = float(m.group(2).replace(".", "").replace(",", ".") if "," in m.group(2) else m.group(2))
            chains = tuple(c for c in re.split(r"[\s,+]+", m.group(3).lower()) if c)
            if capital <= 0 or any(c not in CHAINS for c in chains):
                raise ValueError("capital o red")
        except ValueError:
            warnings.append(f"{path.name} línea {n}: '{raw}' no tiene el formato «PAR CAPITAL [redes]» "
                            f"(redes: {', '.join(sorted(CHAINS))}); se ignora")
            continue
        out.append(Watch(f"{base}/{quote}", capital, chains))
    return out, warnings


def check(watch: Watch, analyze=None) -> dict:
    """Estado de una vigilancia: la mejor propuesta de hoy (si la hay) o la mejor combinación
    descartada, con su motivo."""
    try:
        rep = (analyze or op.analyze)(watch.pair, watch.capital, watch.chains or None)
    except Exception as e:      # una vigilancia que falla no detiene la rutina
        return {"clave": watch.key, "par": watch.pair, "capital": watch.capital, "redes": list(watch.chains),
                "estado": "", "hay_propuesta": False, "texto": f"no se pudo analizar ({type(e).__name__})", "error": True}
    base = {"clave": watch.key, "par": f"{rep.base}/{rep.quote}" if rep.quote else watch.pair,
            "capital": watch.capital, "redes": list(watch.chains), "error": bool(rep.error)}
    if rep.error:
        return base | {"estado": "", "hay_propuesta": False, "texto": rep.error}
    if rep.proposals:
        o = rep.proposals[0]
        q = o.quote
        return base | {
            "estado": f"{o.name} · {o.pool.label}", "hay_propuesta": True,
            "texto": (f"propuesta: ciclo {o.name.lower()} en {o.pool.label}, rango {op._p(q.low)} – {op._p(q.high)}, "
                      f"neto estimado {o.net_cycle:+,.2f} $ por ciclo ({o.net_apr:+.1%} anual; con margen "
                      f"{o.net_safe:+,.2f} $)"),
            "enlace": o.pool.app_url or o.pool.url}
    scored = [o for o in rep.opportunities if o.net_cycle is not None]
    if not scored:
        why = "; ".join(rep.warnings) or "ningún pool con liquidez suficiente"
        return base | {"estado": "", "hay_propuesta": False, "texto": f"sin propuesta: {why}"}
    best = max(scored, key=lambda o: o.net_apr)
    return base | {"estado": "", "hay_propuesta": False,
                   "texto": (f"sin propuesta. Lo más cercano: ciclo {best.name.lower()} en {best.pool.label}, neto "
                             f"estimado {best.net_cycle:+,.2f} $ por ciclo ({best.excluded})")}


def alerts(prev: dict, items: list[dict]) -> list[dict]:
    """Avisa cuando una vigilancia pasa a tener propuesta, cambia de propuesta o la pierde. La
    primera vez que se ve una vigilancia solo avisa si ya hay propuesta."""
    out = []
    for i in items:
        if i.get("error"):
            continue
        old = prev.get(i["clave"])
        if i["estado"] == (old or ""):
            continue
        if i["hay_propuesta"]:
            text = i["texto"]
        elif old:
            text = f"la propuesta anterior ({old}) ya no cumple los criterios. {i['texto']}"
        else:
            continue
        out.append({"simbolo": i["par"], "marco": f"oportunidad con {i['capital']:,.0f} $", "tipo": "oportunidad",
                    "texto": text})
    return out
