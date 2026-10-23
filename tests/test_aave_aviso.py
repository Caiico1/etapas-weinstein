"""Aviso de factor de salud en Aave: umbrales, histéresis y gestión de issues (sin red ni GitHub)."""
import pytest

from etapas import aave_aviso as AV
from etapas import aave_posicion as AP

ADDR = "0xD5809aA352938DD7c2C0a16FC3d4013e0563eE27"


def _pos(hf, debt=4_500.0, chain="arbitrum"):
    return AP.Position(ADDR, chain, chain, 0, "", 10_000.0, debt, hf, hf, debt / 10_000, 0.2, None, 0.0, [])


@pytest.mark.parametrize("hf,prev,new", [
    (2.0, "ok", "ok"), (1.45, "ok", "aviso"), (1.2, "ok", "urgente"), (1.2, "aviso", "urgente"),
    (1.55, "aviso", "aviso"),        # entre 1,5 y 1,6 se mantiene
    (1.55, "ok", "ok"),
    (1.45, "urgente", "urgente"),    # no baja de urgente a aviso
    (1.65, "urgente", "ok"), (float("inf"), "aviso", "ok"),
])
def test_level(hf, prev, new):
    assert AV.level(hf, prev) == new


def test_action():
    assert AV.action("ok", "aviso") == "abrir"
    assert AV.action("aviso", "urgente") == "escalar"
    assert AV.action("urgente", "ok") == "cerrar"
    assert AV.action("aviso", "aviso") is None


def test_repay_lleva_al_objetivo():
    p = _pos(1.4)
    repay = AV.repay_to(p)
    assert p.health_recalc * p.debt_usd / (p.debt_usd - repay) == pytest.approx(AV.OBJETIVO)
    assert AV.repay_to(_pos(2.5)) == 0


def test_body_lleva_la_marca_para_encontrar_la_issue():
    p = _pos(1.25)
    b = AV.body(p, "urgente")
    assert AV.marker(AV.key(p)) in b and "devuelve deuda ahora" in b
    assert AV.title(p, "urgente").startswith("URGENTE")


def test_no_publica_direccion_ni_importes():
    """El repositorio es público: ni la dirección ni los $ salen en la issue, la clave o el log."""
    p = _pos(1.25)
    texto = AV.title(p, "urgente") + AV.body(p, "urgente") + AV.key(p)
    assert ADDR.lower()[2:12] not in texto.lower() and ADDR[-4:] not in texto
    assert " $" not in texto and "4.500" not in texto
    assert "de la deuda" in AV.body(p, "aviso")


class FakeGitHub:
    def __init__(self, issues=None):
        self.issues, self.calls = issues or {}, []

    def open_issues(self):
        return self.issues

    def ensure_labels(self):
        pass

    def create(self, title, body, urgent):
        self.calls.append(("create", urgent))

    def escalate(self, n, title, body):
        self.calls.append(("escalate", n))

    def close(self, n, comment):
        self.calls.append(("close", n))


def _run(monkeypatch, positions, gh, failed=()):
    monkeypatch.setattr(AV, "read_positions", lambda addrs: (positions, list(failed)))
    return AV.run([ADDR], gh)


def test_abre_una_sola_vez(monkeypatch):
    gh = FakeGitHub()
    _run(monkeypatch, [_pos(1.4)], gh)
    assert gh.calls == [("create", False)]
    k = AV.key(_pos(1.4))
    gh2 = FakeGitHub({k: AV.Issue(7, k, False)})
    assert _run(monkeypatch, [_pos(1.38)], gh2) == [] and gh2.calls == []


def test_escala_y_cierra(monkeypatch):
    k = AV.key(_pos(1.4))
    gh = FakeGitHub({k: AV.Issue(7, k, False)})
    _run(monkeypatch, [_pos(1.2)], gh)
    assert gh.calls == [("escalate", 7)]
    gh = FakeGitHub({k: AV.Issue(7, k, True)})
    _run(monkeypatch, [_pos(1.7)], gh)
    assert gh.calls == [("close", 7)]


def test_cierra_si_ya_no_hay_posicion_pero_no_si_fallo_la_lectura(monkeypatch):
    k = AV.key(_pos(1.4))
    gh = FakeGitHub({k: AV.Issue(7, k, False)})
    _run(monkeypatch, [], gh, failed=[k])
    assert gh.calls == []
    _run(monkeypatch, [], gh)
    assert gh.calls == [("close", 7)]


def test_sin_deuda_no_avisa(monkeypatch):
    gh = FakeGitHub()
    assert _run(monkeypatch, [_pos(float("inf"), debt=0.0)], gh) == [] and gh.calls == []
