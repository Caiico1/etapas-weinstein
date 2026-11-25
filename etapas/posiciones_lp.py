"""Tus posiciones de liquidez abiertas, simuladas desde hoy con el simulador de pools.

    python -m etapas.posiciones_lp                 # las carteras de ~/.cripto-renta/config.json, 30 días
    python -m etapas.posiciones_lp --dias 90 0x…   # otra cartera u otro plazo

Lee las posiciones abiertas del MCP público de Revert (solo lectura, sin clave: posiciones, ticks,
valor y comisiones pendientes), busca su pool en MetaProDB y simula lo que queda con su rango y su
valor reales, sin gas ni cambio inicial (ya está abierta). «vs mantener» es aquí frente a cerrarla hoy
y guardar los tokens que tiene ahora. También da el mejor ancho del buscador para ese pool, que
costaría cerrar y volver a abrir.

Las direcciones no se guardan en el repositorio (es público): salen de ~/.cripto-renta/config.json
(«carteras»), como en `etapas.aave_posicion`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

from . import simulador as S
from .aave_posicion import addresses_from_config

REVERT_MCP = "https://mcp.revert.finance/mcp"
REDES = {"mainnet": "ethereum", "arbitrum": "arbitrum", "base": "base"}
PROTOCOLOS = {"uniswapv3": "v3", "uniswapv4": "v4"}
POSICIONES_TXT = Path(__file__).resolve().parent.parent / "posiciones.txt"
# Stop elegido a mano para una posición (local, fuera del repo): {"n.º de posición": "regla de S.SALIDAS"}.
# Sin entrada, se vigila el stop que sugiere el simulador.
STOPS = Path.home() / ".cripto-renta" / "stops.json"


def stops_elegidos(path: Path = STOPS) -> dict[str, str]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in d.items() if v in S.SALIDAS and S.SALIDAS[v] is not None}


class RevertMCP:
    """Cliente mínimo del MCP de Revert por HTTP (JSON-RPC, «streamable HTTP»)."""

    def __init__(self, url: str = REVERT_MCP, timeout: int = 60):
        self.url, self.timeout, self.sid, self.n = url, timeout, None, 0

    def _post(self, body: dict) -> dict | None:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "etapas-simulador/1.0"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), h, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            raw, ct = r.read().decode("utf-8"), r.headers.get("Content-Type", "")
        if "id" not in body:
            return None
        if "event-stream" in ct:
            raw = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")][-1]
        out = json.loads(raw)
        if "error" in out:
            raise RuntimeError(f"Revert: {out['error']}")
        return out["result"]

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        return self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def conectar(self) -> None:
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "etapas-simulador", "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, **args) -> dict:
        if self.sid is None:
            self.conectar()
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        text = res["content"][0]["text"]
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError(f"Revert {name}: {text[:300]}") from None
        if not data.get("success", True):
            raise RuntimeError(f"Revert {name}: {data}")
        return data

    def posiciones(self, cuenta: str) -> list[dict]:
        out = []
        for proto in PROTOCOLOS:                 # por cuenta, Revert devuelve hasta 100 por protocolo
            d = self.tool("get_positions", account=cuenta, protocol=proto)
            out += d.get("data") or []
            if (d.get("pagination") or {}).get("has_next"):
                print(f"aviso: Revert tiene más de 100 posiciones {proto} en la cuenta; solo se leen 100", file=sys.stderr)
        return out


def normalizar(p: dict) -> dict | None:
    """De la fila de Revert a lo que necesita el simulador (None si no se puede simular)."""
    red, ver = REDES.get(p.get("network")), PROTOCOLOS.get(p.get("protocol"))
    if not red or not ver or p.get("exited"):
        return None
    tok = p.get("tokens") or {}
    s0 = (tok.get(p.get("token0")) or {}).get("symbol", "?")
    s1 = (tok.get(p.get("token1")) or {}).get("symbol", "?")
    hodl = ((p.get("performance") or {}).get("hodl") or {})
    return {"id": int(p["nft_id"]), "red": red, "version": ver, "pool_id": str(p["pool"]).lower(),
            "par": f"{s0}/{s1}", "fee": int(p.get("fee_tier") or 0), "tl": int(p["tick_lower"]), "tu": int(p["tick_upper"]),
            "valor": float(p.get("underlying_value") or 0), "comisiones_pendientes": float(p.get("fees_value") or 0),
            "en_rango_revert": bool(p.get("in_range")),
            "tick": int(p["pool_tick"]) if p.get("pool_tick") is not None else None, "edad_dias": float(p.get("age_days") or 0),
            "vs_mantener_desde_apertura": float(hodl["pnl"]) if "pnl" in hodl else None}


_CACHE: dict = {}


def leer(cuentas: list[str] | None = None, cliente: RevertMCP | None = None, cache_s: int = 600) -> list[dict]:
    """Posiciones abiertas de las cuentas (por defecto, las de la configuración local)."""
    cuentas = [c.lower() for c in (cuentas or addresses_from_config())]
    if not cuentas:
        raise ValueError("no hay carteras: añade «carteras» a ~/.cripto-renta/config.json o pásalas como argumento")
    key = tuple(sorted(cuentas))
    if cliente is None and key in _CACHE and time.time() - _CACHE[key][0] < cache_s:
        return _CACHE[key][1]
    cliente = cliente or RevertMCP()
    out = [n for c in cuentas for p in cliente.posiciones(c) if (n := normalizar(p))]
    out.sort(key=lambda x: -x["valor"])
    _CACHE[key] = (time.time(), out)
    return out


def vigiladas(path: Path = POSICIONES_TXT) -> list[dict]:
    """Líneas activas de posiciones.txt: símbolo, rango de precios y n.º de posición si lo dice la nota."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for l in lines:
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        parts = l.split()
        try:
            lo, hi = sorted((float(parts[1]), float(parts[2])))
        except (IndexError, ValueError):
            continue
        out.append({"simbolo": parts[0], "lo": lo, "hi": hi, "ids": {int(m) for m in re.findall(r"n\.º\s*(\d+)", l)},
                    "texto": " ".join(parts[:3])})
    return out


def _mismo_rango(a: tuple[float, float], b: tuple[float, float], tol: float = 0.005) -> bool:
    return all(abs(x / y - 1) <= tol for x, y in zip(a, b) if y)


def cruzar(filas: list[dict], lineas: list[dict]) -> list[str]:
    """Avisos de lo que no cuadra entre Revert y posiciones.txt (por n.º o, si no lo hay, por rango)."""
    usadas, avisos = set(), []
    for f in filas:
        hit = next((i for i, l in enumerate(lineas) if f["id"] in l["ids"] or
                    ("lo" in f and _mismo_rango((f["lo"], f["hi"]), (l["lo"], l["hi"])))), None)
        if hit is None:
            avisos.append(f"n.º {f['id']} ({f['par']}, {f['red']}) está abierta en Revert pero no en posiciones.txt: "
                          "la rutina no la vigila")
        else:
            usadas.add(hit)
    for i, l in enumerate(lineas):
        if i not in usadas:
            quien = ", ".join(f"n.º {x}" for x in sorted(l["ids"])) or l["texto"]
            avisos.append(f"{quien} está en posiciones.txt pero Revert no la da como abierta: si la cerraste, "
                          "quítala de la vigilancia")
    return avisos


def simular(pos: dict, dias: int = 30, escenarios: int = 1000, cargar=None) -> dict:
    """Lo que queda de una posición abierta, desde hoy, con su rango y su valor."""
    cargar = cargar or (lambda p, c, hh: S.load(p, c, hh))
    base = {k: pos[k] for k in ("id", "red", "version", "pool_id", "par", "fee", "valor", "comisiones_pendientes",
                                "edad_dias", "vs_mantener_desde_apertura")}
    try:
        h = cargar(pos["pool_id"], pos["red"], S.HISTORIA_DIAS)
    except Exception as e:                                       # noqa: BLE001
        return base | {"error": f"el pool no está en MetaProDB ({type(e).__name__}: {e})"}
    paths = S.bootstrap(h, dias * 24, escenarios)
    rg = S.rango_de_ticks(h, pos["tl"], pos["tu"])
    s = S.simulate(h, paths, rg, capital=max(pos["valor"], 1e-6), abierta=True)
    r = S.resumen(s)
    g = r["desglose"]
    p_hoy = S.price_now(h)
    best = S.mejor_rango(S.barrido(h, paths, max(pos["valor"], 1e-6)), "media")
    sal = S.comparar_salidas(h, dias, [rg], paths, max(pos["valor"], 1e-6), abierta=True)["rangos"][0]
    st = stop_de(h.pool, pos, sal, stops_elegidos().get(str(pos["id"])))
    return base | {
        "label": h.pool["label"], "dias": dias, "precio": p_hoy, "lo": rg.lo, "hi": rg.hi,
        "en_rango_hoy": rg.lo <= p_hoy <= rg.hi,
        "sale_del_rango": float(np.mean(s.in_range < 0.999)), "en_rango": r["en_rango"], "acaba": r["acaba"],
        "comisiones": r["comisiones"]["p50"], "comisiones_media": g["comisiones"],
        "vs_cerrar_media": g["total"] - g["precio"], "vs_cerrar_mediana": r["vs_mantener"]["p50"],
        "vs_cerrar_p10": r["vs_mantener"]["p10"], "valor_final": r["valor_final"],
        "fiabilidad": S.fiabilidad(h, dias, S.calibracion_pool(h.pool["pool_id"], h.pool["chain"], dias)),
        "mejor_ancho": {"texto": best["texto"], "vs_media_por_valor": best["vs_media"]},
        "salida": sal, "stop": st,
        "lectura": lectura(g["total"] - g["precio"], r["vs_mantener"]["p50"], rg.lo <= p_hoy <= rg.hi, dias, pos["valor"])}


def stop_de(pool: dict, pos: dict, sal: dict, regla: str | None = None) -> dict:
    """El stop que se vigila: `regla` (de S.SALIDAS) si se da, si no el sugerido por el simulador y, si no
    sugiere ninguno, el borde de abajo («sugerido» False: solo avisa de que se sale). Con su nivel en ticks
    y, si Revert da el tick de hoy, su estado (lejos / cerca / cruzado)."""
    sugerido = sal.get("sugerido")
    nombre = regla or sugerido or "al salirse por abajo"
    fila = next(x for x in sal["reglas"] if x["regla"] == nombre)
    nv = S.nivel_stop(pool, pos["tl"], pos["tu"], fila["x"])
    out = {"regla": nombre, "x": fila["x"], "sugerido": bool(sugerido) and nombre == sugerido, "elegido": regla is not None,
           "precio": nv["precio"], "nivel": nv, "prob_salta": fila["prob_salta"], "dif_media": fila["dif_media"],
           "p10_sin": sal["reglas"][0]["valor_p10"], "p10_con": fila["valor_p10"]}
    if pos.get("tick") is not None:
        out["estado"] = S.estado_stop(pos["tick"], nv)
    return out


def dinero(x: float, valor: float | None = None, signo: bool = True) -> str:
    """Dólares con céntimos si es poco dinero, y el % sobre el valor de la posición si se da."""
    d = 2 if abs(x) < 100 else 0
    s = (f"{x:+,.{d}f}" if signo else f"{x:,.{d}f}").replace(",", "X").replace(".", ",").replace("X", ".").replace("-", "−")
    s += " $"
    if valor:
        pct = f"{x / valor * 100:+.1f}" if signo else f"{x / valor * 100:.1f}"
        s += f" ({pct} %)".replace(".", ",").replace("-", "−")
    return s


def lectura(vs_media: float, vs_mediana: float, en_rango: bool, dias: int, valor: float) -> str:
    if not en_rango:
        return (f"Fuera de rango: no cobra. A {dias} días, frente a cerrarla y guardar los tokens: "
                f"{dinero(vs_media, valor)} de media (solo si el precio vuelve a entrar).")
    if vs_media > 0:
        return (f"Mantenerla compensa en los escenarios: {dinero(vs_media, valor)} de media frente a cerrarla "
                "y guardar los tokens.")
    if vs_mediana > 0.001 * valor:
        return (f"Dudosa: en el caso típico gana {dinero(vs_mediana, valor)}, pero de media pierde "
                f"{dinero(-vs_media, valor, False)} frente a cerrarla y guardar los tokens (riesgo de que el precio se escape).")
    return f"No compensa en los escenarios: {dinero(vs_media, valor)} de media frente a cerrarla y guardar los tokens."


def informe(dias: int = 30, escenarios: int = 1000, cuentas: list[str] | None = None, cargar=None,
            cliente: RevertMCP | None = None, lineas: list[dict] | None = None) -> dict:
    pos = leer(cuentas, cliente)
    filas = [simular(p, dias, escenarios, cargar) for p in pos]
    avisos = cruzar(filas, vigiladas() if lineas is None else lineas)
    return {"dias": dias, "posiciones": filas, "avisos": avisos,
            "total_valor": sum(p["valor"] for p in pos), "total_pendientes": sum(p["comisiones_pendientes"] for p in pos)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cuentas", nargs="*")
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--escenarios", type=int, default=1000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        inf = informe(a.dias, a.escenarios, a.cuentas or None)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(inf, ensure_ascii=False, indent=1, default=str))
        return 0
    print(f"{len(inf['posiciones'])} posiciones abiertas · {dinero(inf['total_valor'], signo=False)} · comisiones "
          f"pendientes {dinero(inf['total_pendientes'], signo=False)} · simuladas a {a.dias} días")
    for p in inf["posiciones"]:
        if "error" in p:
            print(f"\nn.º {p['id']} {p['par']} {p['red']} {p['version']}: {p['error']}")
            continue
        f = p["fiabilidad"]["nivel"]
        v = p["valor"]
        print(f"\nn.º {p['id']} · {p['label']} ({p['red']} {p['version']}) · {dinero(v, signo=False)} · "
              f"rango {S._fmt_price(p['lo'])}–{S._fmt_price(p['hi'])}, hoy {S._fmt_price(p['precio'])} "
              f"({'en rango' if p['en_rango_hoy'] else 'FUERA'}) · fiabilidad {f}")
        print(f"  se sale del rango en algún momento: {p['sale_del_rango']:.0%} · dentro {p['en_rango']:.0%} del tiempo · "
              f"comisiones {dinero(p['comisiones'], v, False)} · vs cerrar hoy: media {dinero(p['vs_cerrar_media'], v)}, "
              f"mediana {dinero(p['vs_cerrar_mediana'], v)}")
        print(f"  {p['lectura']}")
        print(f"  mejor ancho del buscador en este pool: {p['mejor_ancho']['texto']} (habría que cerrar y reabrir)")
    for a_ in inf["avisos"]:
        print(f"\n⚠ {a_}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
