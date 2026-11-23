"""Simulador de escenarios: paridad con el backtest y propiedades del bootstrap (series sintéticas, sin BD)."""
import json
import math

import numpy as np
import pandas as pd
import pytest

from etapas import backtest as B
from etapas import simulador as S

POOL = {"chain": "base", "version": "v3", "pool_id": "0x0", "label": "WETH/USDC 0.05%", "fee": 500,
        "tick_spacing": 10, "symbol0": "WETH", "symbol1": "USDC", "dec0": 18, "dec1": 6}
POOL_INV = {**POOL, "label": "USDC/WETH 0.05%", "symbol0": "USDC", "symbol1": "WETH", "dec0": 6, "dec1": 18}


def _walk(n=24 * 40, seed=1, vol=0.004, g0=2e-14, g1=4e-5, pool=POOL, eth0=2000.0):
    rng = np.random.default_rng(seed)
    eth = eth0 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    if pool["symbol0"] == "WETH":
        p_raw = eth / 10 ** (pool["dec0"] - pool["dec1"])
        usd0, usd1 = eth, np.ones(n)
    else:
        p_raw = (1 / eth) / 10 ** (pool["dec0"] - pool["dec1"])
        usd0, usd1 = np.ones(n), eth
    sp = np.sqrt(p_raw)
    ticks = np.floor(np.log(p_raw) / math.log(B.TICK_BASE)).astype(int)
    k = rng.uniform(0.2, 3, n)
    return pd.DataFrame({"hour": pd.date_range("2026-01-01", periods=n, freq="h"), "tick": ticks,
                         "sqrt_price": sp, "usd0": usd0, "usd1": usd1,
                         "fee_growth0": g0 * k, "fee_growth1": g1 * k, "lp_fee": 500,
                         "liquidity": 1e30})


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
@pytest.mark.parametrize("width", [0.03, 0.10, 0.30])
def test_paridad_con_el_backtest_al_reproducir_el_historico(pool, width):
    df = _walk(pool=pool)
    h = S.features(pool, df)
    h.liquidity_now = 0                                     # sin dilución, como el backtest
    tl, tu = B._range(int(df["tick"].iloc[0]), width, pool["tick_spacing"])
    rg = S.Rango("x", tl, tu, 0, 0)
    b = B.simulate(pool, df, B.Strategy(width, gas_usd=0.05), capital=5000)
    s = S.simulate(h, S.replay(h), rg, capital=5000, gas=0.05)
    assert float(s.final[0]) == pytest.approx(b.final, rel=1e-9)
    assert float(s.fees[0]) == pytest.approx(b.fees_usd, rel=1e-9)
    assert float(s.in_range[0]) == pytest.approx(b.in_range_pct, rel=1e-12)
    assert s.costs == pytest.approx(b.costs_usd, rel=1e-12)
    assert float(s.final[0] / s.hodl[0] - 1) == pytest.approx(b.vs_hodl_pct, rel=1e-9, abs=1e-12)


def test_bootstrap_neutral_no_tiene_tendencia_y_es_reproducible():
    df = _walk(n=24 * 200, vol=0.003)
    df["sqrt_price"] *= np.exp(np.arange(len(df)) * 1e-4)    # tendencia fuerte al alza en el histórico
    h = S.features(POOL, df)
    p = S.bootstrap(h, 24 * 30, 3000, tendencia="neutral", seed=3)
    drift = np.log(p.sp[:, -1] / p.sp[:, 0]).mean()
    assert abs(drift) < 0.01
    p_hist = S.bootstrap(h, 24 * 30, 3000, tendencia="historica", seed=3)
    assert np.log(p_hist.sp[:, -1] / p_hist.sp[:, 0]).mean() > 0.02
    p2 = S.bootstrap(h, 24 * 30, 3000, tendencia="neutral", seed=3)
    assert np.array_equal(p.sp, p2.sp)
    assert p.sp.shape == (3000, 24 * 30 + 1) and p.q0.shape == (3000, 24 * 30)


def test_comisiones_invariantes_a_la_escala_del_precio():
    """Las mismas horas reutilizadas con el precio a la mitad cobran la misma proporción del capital."""
    df = _walk()
    half = df.copy()
    half["sqrt_price"] /= math.sqrt(2)
    half["usd0"] /= 2
    half["fee_growth0"] *= math.sqrt(2)                      # g0·√P invariante
    half["fee_growth1"] /= math.sqrt(2)                      # g1/√P invariante
    a, b = S.features(POOL, df), S.features(POOL, half)
    a.liquidity_now = b.liquidity_now = 0
    ra, rb = S.parse_rango("±10%", a), S.parse_rango("±10%", b)
    sa = S.simulate(a, S.replay(a), ra, gas=0)
    sb = S.simulate(b, S.replay(b), rb, gas=0)
    assert float(sb.fees[0]) == pytest.approx(float(sa.fees[0]), rel=0.02)


def test_dilucion_reduce_las_comisiones_en_proporcion():
    df = _walk()
    h = S.features(POOL, df)
    rg = S.parse_rango("±10%", h)
    h.liquidity_now = 0
    full = float(S.simulate(h, S.replay(h), rg, gas=0).fees[0])
    s0 = S.simulate(h, S.replay(h), rg, gas=0)
    L = (5000 - s0.costs) / (sum(x * y for x, y in zip(B._amounts(h.df["sqrt_price"].iloc[0], B._sqrt(rg.tl), B._sqrt(rg.tu)),
                                                         (h.df["sqrt_price"].iloc[0] ** 2, 1))) / 1e6)
    h.liquidity_now = L                                      # tanta liquidez como la nuestra → la mitad
    assert float(S.simulate(h, S.replay(h), rg, gas=0).fees[0]) == pytest.approx(full / 2, rel=1e-6)


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_parse_rango_en_precio_y_en_porcentaje(pool):
    h = S.features(pool, _walk(pool=pool))
    p = S.price_now(h)
    assert 1000 < p < 5000                                   # ETH en USDC, sea cual sea el orden de tokens
    r = S.parse_rango("±10%", h)
    assert r.lo <= p * 0.9 <= r.lo * 1.002 and r.hi >= p * 1.1 >= r.hi / 1.002
    r2 = S.parse_rango(f"{p * 0.8:.2f}-{p * 1.2:.2f}", h)
    assert r2.lo == pytest.approx(p * 0.8, rel=2e-3) and r2.hi == pytest.approx(p * 1.2, rel=2e-3)
    r3 = S.parse_rango("-5/+15", h)
    assert r3.lo == pytest.approx(p * 0.95, rel=2e-3) and r3.hi == pytest.approx(p * 1.15, rel=2e-3)
    assert r.tl % 10 == 0 and r.tu % 10 == 0
    with pytest.raises(ValueError):
        S.parse_rango("1900-1900", h)


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_rango_por_percentiles_del_precio_final(pool):
    h = S.features(pool, _walk(pool=pool, n=24 * 120))
    paths = S.bootstrap(h, 24 * 30, 2000, seed=5)
    r = S.parse_rango("P10-P90", h, paths)
    s = S.simulate(h, paths, r, gas=0)
    assert 0.77 <= float(np.mean(s.side == 0)) <= 0.83          # acaba dentro ~80 % (ajuste a ticks: algo más)
    assert r.lo < S.price_now(h) < r.hi
    ancho = S.parse_rango("P5–P95", h, paths)
    assert ancho.lo <= r.lo and ancho.hi >= r.hi
    with pytest.raises(ValueError):
        S.parse_rango("P10-P90", h)                              # sin escenarios no se puede
    with pytest.raises(ValueError):
        S.parse_rango("P0-P90", h, paths)


def test_factor_plazo_corrige_las_comisiones_segun_el_plazo():
    assert S.factor_plazo(30) == pytest.approx(1.0)
    assert S.factor_plazo(7) > 1 > S.factor_plazo(90)
    assert S.factor_plazo(180) == pytest.approx(6 ** -S.EXPONENTE_PLAZO)
    assert S.factor_plazo(1) == S.factor_plazo(7)                # fuera de lo validado, se limita
    assert S.factor_plazo(90, 0) == 1.0
    h = S.features(POOL, _walk(n=24 * 200))
    con = S.bootstrap(h, 24 * 90, 200, ajuste=1.0, nivel_dias=None, seed=2)
    sin = S.bootstrap(h, 24 * 90, 200, ajuste=1.0, nivel_dias=None, seed=2, exponente=0)
    assert con.q0 == pytest.approx(sin.q0 * S.factor_plazo(90))


@pytest.mark.parametrize("pool", [POOL, POOL_INV])
def test_desglose_suma_el_resultado_y_vs_mantener(pool):
    h = S.features(pool, _walk(pool=pool, n=24 * 120))
    paths = S.bootstrap(h, 24 * 30, 500, seed=3)
    s = S.simulate(h, paths, S.parse_rango("±5%", h))
    r = S.resumen(s)
    g = r["desglose"]
    assert g["precio"] + g["perdida_impermanente"] + g["comisiones"] + g["costes"] == pytest.approx(g["total"])
    assert g["total"] == pytest.approx(float(np.mean(s.final - s.capital)))
    vm = g["perdida_impermanente"] + g["comisiones"] + g["costes"]
    assert vm == pytest.approx(float(np.mean(s.final - s.hodl)))
    assert g["perdida_impermanente"] <= 1e-9 and g["comisiones"] > 0 and g["costes"] < 0


def _hist_fiab(dias_hist, tvl, pool=POOL):
    h = S.features(pool, _walk(n=24 * dias_hist))
    h.tvl_now = tvl
    return h


def test_fiabilidad_semaforo():
    alta = S.fiabilidad(_hist_fiab(740, 5e6), 30)
    assert alta["nivel"] == "alta" and alta["a_favor"]
    assert S.fiabilidad(_hist_fiab(400, 5e6), 30)["nivel"] == "media"         # poco histórico
    assert S.fiabilidad(_hist_fiab(740, 1e6), 30)["nivel"] == "media"         # TVL bajo
    assert S.fiabilidad(_hist_fiab(740, 5e6), 180)["nivel"] == "media"        # plazo largo
    assert S.fiabilidad(_hist_fiab(100, 5e6), 30)["nivel"] == "baja"
    assert S.fiabilidad(_hist_fiab(740, 1e5), 30)["nivel"] == "baja"
    h = _hist_fiab(740, 5e6)
    ok = {"dias": 30, "comisiones": 1.05, "cobertura": 0.8, "pruebas": 50}
    assert S.fiabilidad(h, 30, ok)["nivel"] == "alta"
    assert S.fiabilidad(h, 30, {**ok, "comisiones": 0.6})["nivel"] == "media"
    assert S.fiabilidad(h, 30, {**ok, "comisiones": 0.4})["nivel"] == "baja"


def test_calibracion_pool_lee_el_plazo_mas_parecido(tmp_path):
    log = tmp_path / "cal.jsonl"
    pools = lambda r: [{"pool": "X", "red": "base", "pool_id": "0xabc", "pruebas": 9, "comisiones": r, "cobertura": 0.8}]
    rec = {"fecha": "2026-12-01", "plazos": [{"dias": 7, "pools": pools(1.1)}, {"dias": 90, "pools": pools(0.9)}]}
    log.write_text('{"fecha": "viejo", "ratio": 1}\n' + json.dumps(rec) + "\n", encoding="utf-8")
    assert S.calibracion_pool("0xabc", "base", 60, log)["dias"] == 90
    assert S.calibracion_pool("0xabc", "base", 14, log)["comisiones"] == 1.1
    assert S.calibracion_pool("0xdef", "base", 30, log) is None
    assert S.calibracion_pool("0xabc", "base", 30, tmp_path / "no.jsonl") is None


def test_conclusiones_dicen_si_compensa_frente_a_mantener_y_a_aave():
    def rg(texto, vm, vf, en=0.9):
        return {"texto": texto, "vs_mantener": {"p50": vm}, "valor_final": {"p50": vf, "p10": 4500},
                "prob_gana_mantener": 0.6, "prob_perdida": 0.4, "en_rango": en}
    res = {"capital": 5000, "dias": 30, "rangos": [rg("±5%", -50, 5040, 0.4), rg("±20%", -10, 5030)],
           "sin_riesgo": {"nombre": "USDC en Aave base", "valor_final": 5021},
           "fiabilidad": {"nivel": "media", "motivos": ["TVL bajo"]}}
    c = S.conclusiones(res)
    assert "Ningún rango supera a mantener" in c[0] and "±20%" in c[0]
    assert "supera en la mediana a USDC en Aave" in c[1]
    assert any("±5%" in t and "60 %" in t for t in c) and c[-1].startswith("Fiabilidad media")
    res["rangos"] = [rg("±10%", 30, 5010)]
    c = S.conclusiones(res)
    assert "el mejor es ±10%" in c[0] and "más que la mediana" in c[1]
    cola = rg("±5%", -50, 5080)
    cola["desglose"] = {"total": -192, "perdida_impermanente": -340}
    res["rangos"] = [cola]
    assert any(t.startswith("Ojo con ±5%") and "192 $" in t for t in S.conclusiones(res))


def test_barrido_de_anchos_y_mejor_rango():
    h = S.features(POOL, _walk(n=24 * 200))
    paths = S.bootstrap(h, 24 * 30, 400, seed=4)
    curva = S.barrido(h, paths)
    anchos = [r["ancho"] for r in curva]
    assert anchos == sorted(anchos) and len({(r["lo"], r["hi"]) for r in curva}) == len(curva)
    assert min(anchos) >= S.ancho_minimo(h) > 0.05                       # 3 × vol. diaria (≈ 2 %) ≈ 6 %
    assert len(S.barrido(h, paths, min_ancho=0)) > len(curva)
    assert curva[0]["en_rango"] < curva[-1]["en_rango"]                  # más ancho, más tiempo dentro
    m = S.mejor_rango(curva, "media")
    assert m["vs_media"] == max(r["vs_media"] for r in curva)
    assert S.mejor_rango(curva, "mediana")["vs_mediana"] == max(r["vs_mediana"] for r in curva)
    r = S.resumen(S.simulate(h, paths, S.parse_rango(m["texto"], h)))
    g = r["desglose"]
    assert g["total"] - g["precio"] == pytest.approx(m["vs_media"])       # el texto propuesto reproduce el ancho


def test_par_de_agrupa_equivalentes():
    assert S.par_de(POOL) == ("ETH", "USD")
    assert S.par_de(POOL_INV) == ("ETH", "USD")
    assert S.par_de({**POOL, "symbol0": "cbBTC", "symbol1": "USDT"}) == ("BTC", "USD")


def test_comparar_misma_estrategia_en_los_pools_del_par():
    otro = {**POOL, "pool_id": "0x1", "chain": "ethereum", "label": "WETH/USDT 0.3%", "symbol1": "USDT", "tvl": 9e6}
    chico = {**POOL, "pool_id": "0x2", "tvl": 5e4}
    btc = {**POOL, "pool_id": "0x3", "symbol0": "WBTC", "tvl": 9e6}
    cand = [{**POOL, "tvl": 5e6}, otro, chico, btc]
    series = {"0x0": _walk(seed=1), "0x1": _walk(seed=1, g0=4e-14, g1=8e-5)}
    cargar = lambda p, c, hh: S.features(next(x for x in cand if x["pool_id"] == p), series[p])
    res = S.comparar("0x0", "base", "±10%", dias=14, escenarios=200, cargar=cargar, candidatos=cand)
    assert res["par"] == "ETH/USD"
    assert {f["pool_id"] for f in res["pools"]} == {"0x0", "0x1"}        # ni el de poco TVL ni el de BTC
    assert [f["es_este"] for f in res["pools"]].count(True) == 1
    assert res["pools"][0]["rango"]["vs_media"] >= res["pools"][1]["rango"]["vs_media"]
    assert res["pools"][0]["pool_id"] == "0x1"                           # cobra el doble con el mismo precio
    with pytest.raises(ValueError):
        S.comparar("0x9", "base", cargar=cargar, candidatos=cand)


def test_escala_de_volatilidad_reciente_y_por_plazo():
    df = _walk(n=24 * 200, vol=0.004)
    recent = df["hour"] > df["hour"].iloc[-1] - pd.Timedelta(days=30)
    rng = np.random.default_rng(9)
    lp = np.log(df["sqrt_price"].to_numpy() ** 2)
    ret = np.diff(lp)
    ret[recent.to_numpy()[1:]] *= 2                                   # el último mes, el doble de movido
    df["sqrt_price"] = np.sqrt(np.exp(np.concatenate([[lp[0]], lp[0] + np.cumsum(ret)])))
    h = S.features(POOL, df)
    assert 1.5 < h.vol_reciente(30) < 2.0
    assert 1.0 < h.vol_reciente(90) < h.vol_reciente(30)               # 90 días: un mes movido de tres
    assert S.escala_vol(h, 7) == pytest.approx(h.vol_reciente(S.VOL_DIAS))
    assert S.escala_vol(h, 90) == pytest.approx(S.escala_vol(h, 7) * (90 / 7) ** S.EXPONENTE_VOL)
    assert S.escala_vol(h, 180) == S.escala_vol(h, 90)                 # el ensanche se limita a 90 días
    assert S.escala_vol(h, 30, vol_dias=None, exponente=0) == 1.0
    auto = S.bootstrap(h, 24 * 30, 2000, seed=1)
    base = S.bootstrap(h, 24 * 30, 2000, seed=1, escala=1.0)
    ra, rb = np.log(auto.sp[:, -1] / auto.sp[:, 0]), np.log(base.sp[:, -1] / base.sp[:, 0])
    assert ra.std() / rb.std() == pytest.approx(S.escala_vol(h, 30), rel=0.02)
    assert abs(ra.mean()) < 0.01                                       # sigue sin tendencia


def test_side_en_precio_mostrado_para_pool_invertido():
    df = _walk(pool=POOL_INV, n=200, vol=0.0)
    h = S.features(POOL_INV, df)
    p = S.price_now(h)
    rg = S.parse_rango(f"{p * 0.5:.2f}-{p * 0.8:.2f}", h)    # rango por debajo del precio actual
    s = S.simulate(h, S.replay(h), rg, gas=0)
    assert int(s.side[0]) == 1                               # el precio acaba POR ENCIMA del rango


def test_nivel_reciente_reescala_las_comisiones_de_los_escenarios():
    df = _walk(n=24 * 360, vol=0.002)
    recent = df["hour"] > df["hour"].iloc[-1] - pd.Timedelta(days=90)
    df.loc[recent, ["fee_growth0", "fee_growth1"]] *= 0.5        # las comisiones han caído a la mitad
    h = S.features(POOL, df)
    k = h.nivel(90)
    assert 0.5 < k < 0.7                                         # mitad frente a la media del año
    assert h.nivel(None) == 1.0
    con = S.bootstrap(h, 24 * 30, 500, ajuste=1.0, nivel_dias=90, seed=1)
    sin = S.bootstrap(h, 24 * 30, 500, ajuste=1.0, nivel_dias=None, seed=1)
    assert con.q1 == pytest.approx(sin.q1 * k)
    assert np.array_equal(con.sp, sin.sp)                        # el precio no cambia
