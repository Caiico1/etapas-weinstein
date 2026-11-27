"""Plan completo para un pool y un capital: ancho del rango × stop × cobertura con un corto en el perpetuo,
todo en los mismos escenarios, y el que mejor resultado deja.

Cada plan reparte el capital (en la estable) entre el pool y, si se cubre, el margen aislado de un corto en
Bybit por la parte `c` de lo que el pool tiene en activo al abrir (como `perpetuo.cubierta`). Si salta el
stop, se cierran a la vez el pool (a dólares, `simulador.simulate_salida`) y el corto (taker), y todo queda
en dólares rindiendo la referencia sin riesgo hasta el final del plazo. Sin stop ni cobertura es exactamente
`simulador.simulate`; sin stop, `perpetuo.cubierta`; sin cobertura, `simulador.simulate_salida`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import backtest as B
from . import perpetuo as P
from . import simulador as S

ANCHOS_PLAN = (0.05, 0.10, 0.15, 0.20, 0.30, 0.50)
STOPS_PLAN = {"sin stop": None, "al salirse por abajo": 0.0, "5 % bajo el borde": 0.05}
COBERTURAS_PLAN = (0.0, 0.5, 1.0)
CRITERIOS_PLAN = ("media", "prudente")
TOLERANCIA_PRUDENTE = 0.01          # el prudente puede ceder hasta un 1 % del capital de media


def simulate_plan(h: S.History, paths: S.Paths, rg: S.Rango, x: float | None, c: float, capital: float,
                  gas_tx: float, asset0: bool = True, m: float = 1.0, apr: float = 0.0,
                  funding_h: np.ndarray | None = None, apy: float = 0.0) -> dict:
    """Pool (con stop `x`) + corto de la parte `c`, en cada camino. Devuelve final, salta, liquidada y el reparto."""
    Sn, H = paths.sp.shape[0], paths.sp.shape[1] - 1
    sp0, u10 = float(paths.sp[0, 0]), float(paths.usd1[0, 0])
    a0u, a1u = B._amounts(sp0, B._sqrt(rg.tl), B._sqrt(rg.tu))
    v0, v1 = a0u * sp0 ** 2 * u10, a1u * u10
    s = (v0 if asset0 else v1) / (v0 + v1)
    V = capital / (1 + c * s * m)
    N = c * s * V
    M = m * N
    lp = S.simulate_salida(h, paths, rg, x, V, gas_tx, apy=apy)
    salta = lp.reajustes.astype(bool)
    base = {"V": V, "M": M, "N": N, "s": s, "lp": lp, "salta": salta}
    if N <= 0:
        return base | {"final": lp.final, "liquidada": np.zeros(Sn, bool), "funding": np.zeros(Sn)}
    if x is None:
        k = np.full(Sn, H)
    else:
        _, t = S.hora_stop(h, paths, rg, x)
        k = np.where(salta, t + 1, H)                            # punto de los caminos en que se cierra
    pa = S._precio_activo(h, paths.sp, paths.usd1, asset0)
    u = N / pa[:, :1]
    fr = np.broadcast_to(funding_h, (Sn, H)) if funding_h is not None else np.full((Sn, H), apr / 8760)
    fund = np.concatenate([np.zeros((Sn, 1)), np.cumsum(u * pa[:, 1:] * fr, axis=1)], axis=1)
    eq = M - N * P.TAKER + u * (pa[:, :1] - pa) + fund
    malo = eq <= P.MMR * u * pa
    vivo = np.arange(H + 1)[None, :] <= k[:, None]
    liq = (malo & vivo).any(axis=1)
    r = np.arange(Sn)
    cierre = eq[r, k] - u[:, 0] * pa[r, k] * P.TAKER
    resto = (H - k) / 8760
    fin = np.where(liq, 0.0, cierre * (1 + apy) ** resto)
    return base | {"final": lp.final + fin, "liquidada": liq, "funding": np.where(liq, 0.0, fund[r, k])}


def candidatos(h: S.History, cubrir: bool, anchos: tuple = ANCHOS_PLAN) -> list[tuple[float, str, float]]:
    """(ancho, stop, cobertura) a probar: anchos desde el mínimo fiable del pool (como el buscador)."""
    mn = S.ancho_minimo(h)
    ws = [w for w in anchos if w >= mn] or [anchos[-1]]
    cs = COBERTURAS_PLAN if cubrir else (0.0,)
    return [(w, st, c) for w in ws for st in STOPS_PLAN for c in cs]


def elegir(filas: list[dict], criterio: str, capital: float) -> dict:
    """«media»: el de más valor final medio. «prudente»: el de mejor P10 entre los que no ceden más de
    TOLERANCIA_PRUDENTE del capital de media frente al mejor."""
    best = max(filas, key=lambda f: f["valor_media"])
    if criterio == "media":
        return best
    ok = [f for f in filas if f["valor_media"] >= best["valor_media"] - TOLERANCIA_PRUDENTE * capital]
    return max(ok, key=lambda f: f["valor_p10"])


def _fila(w, st, c, r, capital) -> dict:
    fv = r["final"]
    return {"ancho": w, "stop": st, "cobertura": c, "valor_media": float(np.mean(fv)),
            "valor_mediana": float(np.median(fv)), "valor_p10": float(np.percentile(fv, 10)),
            "valor_p90": float(np.percentile(fv, 90)), "prob_perdida": float(np.mean(fv < capital)),
            "prob_stop": float(np.mean(r["salta"])), "prob_liquidacion": float(np.mean(r["liquidada"])),
            "en_pool": r["V"], "margen": r["M"], "corto": r["N"],
            # frente a guardar los mismos tokens que se depositan (solo sin cobertura: con ella, parte va al margen)
            "vs_mantener": float(np.mean(fv - r["lp"].hodl)) if c == 0 else None}


def planes(h: S.History, dias: int, paths: S.Paths, capital: float = 5000, gas: float | None = None,
           apy: float = 0.0, f: pd.Series | None = None, anchos: tuple = ANCHOS_PLAN) -> dict:
    """Todos los planes en los mismos escenarios, el elegido por cada criterio y el de partida (±10 %, sin
    stop ni cobertura) para comparar."""
    gas_tx = S.GAS.get(h.pool["chain"], 0.05) if gas is None else gas
    sim = P.simbolo(h.pool)
    _, _, asset0 = B.pair_of(h.pool)
    cubrir, m, apr, aviso = False, 1.0, 0.0, None
    if sim:
        try:
            f = P.funding(sim[0]) if f is None else f
            asset0 = sim[1]
            m = P.margen(S.hf_historico(h, dias, asset0))
            apr = P.apr_reciente(f, h.hour_now)
            cubrir = True
        except OSError as e:
            aviso = f"sin cobertura: no se pudo leer el funding de {sim[0]} en Bybit ({type(e).__name__})"
    filas = []
    for w, st, c in candidatos(h, cubrir, anchos):
        rg = S.parse_rango(f"{w * 100:g}", h)
        r = simulate_plan(h, paths, rg, STOPS_PLAN[st], c, capital, gas_tx, asset0, m, apr, apy=apy)
        fila = _fila(w, st, c, r, capital) | {"lo": rg.lo, "hi": rg.hi}
        if STOPS_PLAN[st] is not None:
            fila["precio_stop"] = rg.lo * (1 - STOPS_PLAN[st])
        filas.append(fila)
    rg10 = S.parse_rango("10", h)
    base = _fila(0.10, "sin stop", 0.0, simulate_plan(h, paths, rg10, None, 0.0, capital, gas_tx, asset0, apy=apy),
                 capital) | {"lo": rg10.lo, "hi": rg10.hi}
    return {"filas": filas, "base": base, "elegido": {k: elegir(filas, k, capital) for k in CRITERIOS_PLAN},
            "perpetuo": sim[0] if sim and cubrir else None, "margen": m if cubrir else None,
            "apr_funding": apr if cubrir else None, "apy": apy, "aviso": aviso}


# Validado el 27-nov-2026 (24 pools de referencia, aperturas de dos años, 30 y 90 días; en cada fecha todos
# los planes con los datos de antes y lo real de cada uno, con el funding real hora a hora). Frente a ±10 %
# sin stop ni cobertura, el plan «media» ganó en lo real en los cuatro grupos y en los dos años: 30 d +37 $
# (sin perpetuo) y +68 $ (con perpetuo) por 5.000 $; 90 d +106 $ y +229 $. Casi todo viene del ancho: en el
# 70–77 % de las fechas elige el rango más ancho sin más, y la regla fija «el más ancho» da lo mismo. El plan
# «prudente» (ceder ≤ 1 % de media por el mejor P10) solo protege de verdad donde hay perpetuo: P10 real a
# 30 d 4.397 → 4.864 $ frente a «media» (90 d 4.222 → 4.520 $), con un coste que depende del año (30 d:
# −68 $ frente a «media» en el lateral, +62 $ en el bajista). Sin perpetuo apenas mueve el P10 (los stops
# cuestan más de lo que protegen). Lo real quedó dentro del P10–P90 del plan elegido el 76–90 % de las veces.
# Rangos de más de ±50 %, probados y descartados (F3, 27-nov-2026, mismas aperturas): cuanto más ancho, menos
# pierde frente a guardar los mismos tokens, sin óptimo intermedio (30 d: ±10 % −101 $, ±50 % −41 $, ±70 %
# −32 $, ±90 % −25 $ por 5.000 $; 90 d: −267, −110, −81, −62 $; ninguno gana ningún año) y las comisiones
# caen de 166 $ a 38 $ al mes: ensanchar solo acerca a no hacer nada. Ampliar el plan hasta ±90 % mejoró lo
# real solo 5–40 $ y no en los dos años en todos los casos (90 d sin perpetuo, año bajista: −9 $).
PLAZOS_PLAN_VALIDADOS = (30, 90)
MEJORA_P10 = 0.02                  # el prudente se propone si sube el P10 al menos un 2 % del capital


def _describe(f: dict, asset: str, perp: str | None) -> str:
    txt = f"rango ±{f['ancho'] * 100:g} % ({S._fmt_price(f['lo'])}–{S._fmt_price(f['hi'])})"
    extra = []
    if f["stop"] != "sin stop":
        extra.append(f"stop {f['stop']} (vender a dólares si {asset} cierra una hora por debajo de "
                     f"{S._fmt_price(f['precio_stop'])})")
    if f["cobertura"] > 0:
        extra.append(f"corto de {S._usd(f['corto'], False)} en {perp} ({'del todo' if f['cobertura'] >= 1 else 'a medias'}, "
                     f"{S._usd(f['margen'], False)} de margen y {S._usd(f['en_pool'], False)} al pool)")
    return txt + (", " + " y ".join(extra) if extra else ", sin stop ni cobertura")


def frases(p: dict, asset: str, capital: float, dias: int) -> dict:
    """La lectura del plan: el de mejor media, el prudente si protege de verdad, y si está validado."""
    md, pr, b = p["elegido"]["media"], p["elegido"]["prudente"], p["base"]
    out = {"media": (f"Para el mejor resultado medio: {_describe(md, asset, p['perpetuo'])}. De media "
                     f"{S._usd(md['valor_media'] - capital)} en {dias} días ({S._usd(md['valor_media'] - b['valor_media'])} "
                     f"frente a ±10 % sin stop ni cobertura); en un escenario malo (1 de cada 10), "
                     f"{S._usd(md['valor_p10'] - capital)}.")}
    if md.get("vs_mantener") is not None:
        out["media"] += (f" Frente a guardar los mismos tokens, {S._usd(md['vs_mantener'])} de media"
                         + (": ser LP aquí no compensa frente a guardarlos." if md["vs_mantener"] < 0 else "."))
    if md["ancho"] == max(x["ancho"] for x in p["filas"]):
        out["media"] += (" Es el rango más ancho que se prueba. Ensanchar más no cambia la conclusión: en la validación "
                         "±70 % y ±90 % perdían aún menos, pero seguían perdiendo frente a guardar los tokens (−25 $ al mes "
                         "por 5.000 $ con ±90 %) y se acercan a no hacer nada.")
    protege = pr is not md and pr["valor_p10"] - md["valor_p10"] >= MEJORA_P10 * capital
    out["proteger"] = protege
    if protege:
        out["prudente"] = (f"Para protegerte: {_describe(pr, asset, p['perpetuo'])}. Sube el escenario malo de "
                           f"{S._usd(md['valor_p10'] - capital)} a {S._usd(pr['valor_p10'] - capital)} y "
                           f"{'cuesta' if pr['valor_media'] < md['valor_media'] else 'gana'} de media "
                           f"{S._usd(abs(pr['valor_media'] - md['valor_media']), False)} frente al anterior"
                           + (f"; liquidación del corto en el {S._pct(pr['prob_liquidacion'])} de los escenarios"
                              if pr["prob_liquidacion"] > 0.005 else "")
                           + ". Es un seguro: en el año bajista de la validación ganó, en el lateral costó.")
    else:
        out["prudente"] = ("Protegerse aquí no compensa: ningún stop ni cobertura sube el escenario malo un 2 % del "
                           "capital sin ceder más del 1 % de media" + ("" if p["perpetuo"] else
                                                                       " (este par no tiene perpetuo para cubrirlo)") + ".")
    out["validado"] = PLAZOS_PLAN_VALIDADOS[0] <= dias <= PLAZOS_PLAN_VALIDADOS[1]
    return out
