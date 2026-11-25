"""Pantalla local del simulador de pools (sin dependencias: servidor de la librería estándar).

    python -m etapas.simulador_web            # http://localhost:8768

Un solo motor: la página llama a /api/simular, que usa `etapas.simulador.run`.
"""
from __future__ import annotations

import argparse
import json
import threading
import traceback
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import backtest as B
from . import simulador as S
from . import simulador_historial as HIST

PAGE = Path(__file__).with_name("simulador_web.html")
_lock = threading.Lock()


@lru_cache(maxsize=16)
def _history(pool_id: str, chain: str, historia: int, hora: str) -> S.History:
    return S.load(pool_id, chain, historia)


def _hora_actual() -> str:
    import pandas as pd
    return pd.Timestamp.now().floor("h").isoformat()        # la caché se renueva cada hora


def pools() -> list[dict]:
    con = B._connect()
    try:
        rows = con.execute("""
            WITH last AS (SELECT chain, pool_id, max(hour) h FROM pool_hour_v
                          WHERE hour > now() - INTERVAL 3 DAY AND usd0 IS NOT NULL GROUP BY 1, 2)
            SELECT p.chain, p.version, p.pool_id, p.label, v.tvl_usd, v.hour, first.f
            FROM pools p JOIN last USING (chain, pool_id)
            JOIN pool_hour_v v ON v.chain = p.chain AND v.pool_id = p.pool_id AND v.hour = last.h
            JOIN (SELECT chain, pool_id, min(hour) f FROM pool_hour GROUP BY 1, 2) first USING (chain, pool_id)
            ORDER BY v.tvl_usd DESC NULLS LAST""").fetchall()
    finally:
        con.close()
    return [{"chain": r[0], "version": r[1], "pool_id": r[2], "label": r[3], "tvl": r[4],
             "hora": str(r[5]), "desde": str(r[6])[:10]} for r in rows]


def simular(q: dict) -> dict:
    g = lambda k, d: q.get(k, [d])[0]
    pool_id, chain = g("pool_id", ""), g("chain", "")
    historia = int(g("historia", S.HISTORIA_DIAS))
    rangos = json.loads(g("rangos", "[]")) or ["±10%"]
    with _lock:
        h = _history(pool_id, chain, historia, _hora_actual())
    res = S.run(pool_id, chain, rangos, float(g("capital", 5000)), int(g("dias", 30)), historia,
                int(g("escenarios", 1000)), int(g("bloque", 168)), g("tendencia", "neutral"),
                float(g("gas")) if g("gas", "") else None, h=h, ajuste=float(g("ajuste", S.AJUSTE_COMISIONES)),
                nivel_dias=int(g("nivel", S.NIVEL_DIAS)) or None)
    try:                                   # guardarla para compararla con lo real cuando venza (local)
        HIST.registrar(res)
    except Exception:                      # noqa: BLE001 (el historial no debe tumbar la simulación)
        traceback.print_exc()
    return res


def historial(q: dict) -> dict:
    HIST.evaluar()
    r = HIST.resumen()
    return r | {"texto": HIST.texto(r)}


def _comunes(q: dict) -> dict:
    g = lambda k, d: q.get(k, [d])[0]
    return {"pool_id": g("pool_id", ""), "chain": g("chain", ""), "historia": int(g("historia", S.HISTORIA_DIAS)),
            "capital": float(g("capital", 5000)), "dias": int(g("dias", 30)),
            "escenarios": int(g("escenarios", 1000)), "tendencia": g("tendencia", "neutral")}


def _cargar(pool_id: str, chain: str, historia: int) -> S.History:
    with _lock:
        return _history(pool_id, chain, historia, _hora_actual())


def optimizar(q: dict) -> dict:
    c = _comunes(q)
    g = lambda k, d: q.get(k, [d])[0]
    h = _cargar(c["pool_id"], c["chain"], c["historia"])
    return S.optimizar(c["pool_id"], c["chain"], c["capital"], c["dias"], c["historia"], c["escenarios"],
                       tendencia=c["tendencia"], h=h, ajuste=float(g("ajuste", S.AJUSTE_COMISIONES)),
                       nivel_dias=int(g("nivel", S.NIVEL_DIAS)) or None, criterio=g("criterio", "media"))


def comparar(q: dict) -> dict:
    c = _comunes(q)
    g = lambda k, d: q.get(k, [d])[0]
    return S.comparar(c["pool_id"], c["chain"], g("rango", "P10-P90"), c["capital"], c["dias"], c["historia"],
                      min(c["escenarios"], 500), c["tendencia"], float(g("min_tvl", 100_000)), cargar=_cargar)


def posiciones(q: dict) -> dict:
    from . import posiciones_lp as P
    g = lambda k, d: q.get(k, [d])[0]
    return P.informe(int(g("dias", 30)), min(int(g("escenarios", 1000)), 1000),
                     cargar=lambda p, c, hh: _cargar(p, c, hh))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/pools":
                self._json(200, pools())
            elif u.path == "/api/simular":
                self._json(200, simular(parse_qs(u.query)))
            elif u.path == "/api/optimizar":
                self._json(200, optimizar(parse_qs(u.query)))
            elif u.path == "/api/comparar":
                self._json(200, comparar(parse_qs(u.query)))
            elif u.path == "/api/historial":
                self._json(200, historial(parse_qs(u.query)))
            elif u.path == "/api/posiciones":
                self._json(200, posiciones(parse_qs(u.query)))
            else:
                self._send(404, b"no encontrado", "text/plain")
        except (B.BacktestError, ValueError, RuntimeError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:                                   # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8768)
    a = ap.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Simulador de pools en http://localhost:{a.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
