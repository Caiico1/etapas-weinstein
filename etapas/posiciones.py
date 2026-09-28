"""Vigilancia de posiciones de liquidez abiertas (posiciones.txt): dentro, cerca del borde o fuera."""
import re
from dataclasses import dataclass
from pathlib import Path

from .config import LP_EDGE_WARN

_LINE = re.compile(r"^([A-Za-z0-9]{1,15})\s+([0-9][0-9.,]*)\s+([0-9][0-9.,]*)\s*(.*)$")


@dataclass
class Position:
    symbol: str
    low: float
    high: float
    label: str = ""          # texto libre tras los precios (perfil, pool, fecha...)


def _num(s: str) -> float:
    """Admite 2500, 2500.5, 2.500,5 y 2,500.5."""
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def load_positions(path: Path) -> tuple[list[Position], list[str]]:
    """Una posición por línea: «SÍMBOLO MÍN MÁX [nota]». Ignora comentarios (#) y líneas vacías."""
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
                raise ValueError
            low, high = _num(m.group(2)), _num(m.group(3))
            if not 0 < low < high:
                raise ValueError
        except ValueError:
            warnings.append(f"{path.name} línea {n}: '{raw}' no tiene el formato «SÍMBOLO MÍN MÁX»; se ignora")
            continue
        out.append(Position(m.group(1).upper(), low, high, m.group(4).strip()))
    return out, warnings


def check(pos: Position, price: float | None) -> dict:
    """Estado de una posición con el precio actual."""
    base = {"simbolo": pos.symbol, "min": pos.low, "max": pos.high, "nota": pos.label, "precio": price}
    if price is None:
        return base | {"estado": "sin precio", "texto": "no se pudo obtener el precio actual", "aviso": False}
    if price < pos.low:
        return base | {"estado": "fuera por abajo", "aviso": True,
                       "texto": f"fuera del rango por abajo ({price / pos.low - 1:+.1%} bajo el mínimo): la "
                                f"posición es 100 % {pos.symbol} y no cobra comisiones"}
    if price > pos.high:
        return base | {"estado": "fuera por arriba", "aviso": True,
                       "texto": f"fuera del rango por arriba ({price / pos.high - 1:+.1%} sobre el máximo): la "
                                f"posición es 100 % moneda estable y no cobra comisiones"}
    margin = LP_EDGE_WARN * (pos.high - pos.low)
    if price - pos.low < margin:
        return base | {"estado": "cerca del mínimo", "aviso": True,
                       "texto": f"dentro, pero cerca del mínimo (a {price / pos.low - 1:.1%})"}
    if pos.high - price < margin:
        return base | {"estado": "cerca del máximo", "aviso": True,
                       "texto": f"dentro, pero cerca del máximo (a {1 - price / pos.high:.1%})"}
    return base | {"estado": "dentro", "aviso": False, "texto": "dentro del rango, cobrando comisiones"}
