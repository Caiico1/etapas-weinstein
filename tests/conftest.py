"""Ajustes comunes: los tests nunca consultan fuentes de noticias ni pools reales."""
import pytest

from etapas import pools, rutina
from etapas.contexto import MarketContext


@pytest.fixture(autouse=True)
def _sin_red_de_noticias(monkeypatch):
    monkeypatch.setattr(rutina, "load_context", lambda now=None: MarketContext())


@pytest.fixture(autouse=True)
def _sin_red_de_pools(monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("sin red en los tests")
    monkeypatch.setattr(pools, "_http_json", offline)
