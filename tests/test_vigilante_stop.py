import math

import pytest

from etapas import simulador as S
from etapas import vigilante_stop as V

POOL0 = {"symbol0": "WETH", "symbol1": "USDC", "dec0": 18, "dec1": 6, "chain": "base"}       # activo = token0
POOL1 = {"symbol0": "USDC", "symbol1": "WETH", "dec0": 6, "dec1": 18, "chain": "arbitrum"}   # activo = token1
C = math.log(1.03) / math.log(S.B.TICK_BASE)


@pytest.mark.parametrize("pool", [POOL0, POOL1])
def test_nivel_y_estado_del_stop_en_los_dos_sentidos(pool):
    tl, tu = -200_000, -198_000
    nv = S.nivel_stop(pool, tl, tu, 0.05)
    d = math.log(0.95) / math.log(S.B.TICK_BASE)
    if nv["asset0"]:
        assert nv["tick"] == pytest.approx(tl + d)
        lejos, cerca, cruzado = tl + 10, nv["tick"] + C / 2, nv["tick"] - 1
    else:
        assert nv["tick"] == pytest.approx(tu - d)
        lejos, cerca, cruzado = tu - 10, nv["tick"] - C / 2, nv["tick"] + 1
    assert [S.estado_stop(t, nv) for t in (lejos, cerca, cruzado)] == ["lejos", "cerca", "cruzado"]
    # el precio mostrado del stop es el borde de abajo (en precio del activo) × 0,95
    borde = S._display(pool, S.B.TICK_BASE ** (tl if nv["asset0"] else tu))
    assert nv["precio"] == pytest.approx(borde * 0.95, rel=1e-9)
    assert V.precio_de_tick(nv["tick"], nv) == pytest.approx(nv["precio"])
    assert V.precio_de_tick(cruzado, nv) < nv["precio"] < V.precio_de_tick(lejos, nv)


class FakeGH:
    def __init__(self):
        self.n, self.log = None, []

    def open_issue(self):
        return self.n

    def create(self, title, body):
        self.n = 7
        self.log.append(("create", title, body))

    def comment(self, n, body):
        self.log.append(("comment", n, body))

    def close(self, n, comment):
        self.n = None
        self.log.append(("close", n, comment))


def test_run_avisa_solo_al_empeorar_y_la_issue_no_lleva_datos(tmp_path):
    tl, tu = -200_000, -198_000
    pos = {"id": 123456, "par": "WETH/USDC", "red": "base", "tl": tl, "tu": tu, "valor": 1234.5, "tick": tl + 500}
    nv = S.nivel_stop(POOL0, tl, tu, 0.0)
    llamadas = []

    def simular(p, dias, esc):
        llamadas.append(p["id"])
        return {"label": "WETH/USDC 0.05%", "stop": {"regla": "al salirse por abajo", "x": 0.0, "sugerido": True,
                                                     "elegido": False, "precio": nv["precio"], "nivel": nv}}

    gh, avisos = FakeGH(), []
    kw = dict(gh=gh, notificar=lambda t, c: avisos.append((t, c)), leer=lambda cache_s=0: [dict(pos)], simular=simular,
              niveles_path=tmp_path / "n.json", estado_path=tmp_path / "e.json")
    assert "todas lejos" in V.run(**kw) and not avisos and not gh.log
    pos["tick"] = nv["cerca"] - 1                                     # se acerca
    V.run(**kw)
    assert len(avisos) == 1 and avisos[0][0].startswith("Cerca del stop") and gh.log[0][0] == "create"
    V.run(**kw)                                                        # sigue cerca: no repite
    assert len(avisos) == 1 and len(gh.log) == 1
    pos["tick"] = nv["tick"] - 5                                       # lo cruza
    V.run(**kw)
    assert avisos[-1][0].startswith("Stop cruzado") and "Cierra la posición" in avisos[-1][1]
    assert gh.log[-1][0] == "comment"
    pos["tick"] = tl + 500                                             # vuelve: se cierra la issue sin avisar
    V.run(**kw)
    assert len(avisos) == 2 and gh.log[-1][0] == "close"
    assert llamadas == [123456]                                       # el nivel se calcula una vez al día
    for _, *txt in gh.log:                                             # repositorio público
        body = " ".join(map(str, txt))
        assert "123456" not in body and "WETH" not in body and "1234" not in body and f"{nv['precio']:.0f}" not in body


def test_simular_no_guarda_el_estado(tmp_path):
    tl, tu = -200_000, -198_000
    nv = S.nivel_stop(POOL0, tl, tu, 0.0)
    sim = lambda p, d, e: {"label": "x", "stop": {"regla": "al salirse por abajo", "x": 0.0, "sugerido": False,
                                                   "elegido": False, "precio": nv["precio"], "nivel": nv}}
    pos = {"id": 1, "par": "WETH/USDC", "red": "base", "tl": tl, "tu": tu, "tick": tl - 10}
    V.run(None, None, lambda cache_s=0: [pos], sim, tmp_path / "n.json", tmp_path / "e.json", guardar=False)
    assert not (tmp_path / "e.json").exists()
    t, c = V.texto({"id": 1, "estado": "cruzado", "par": "x", "red": "base", "precio": 1.0,
                    "stop": {"regla": "al salirse por abajo", "sugerido": False, "elegido": False, "precio": 1.1}})
    assert "no sugería stop" in c and "Decide" in c
