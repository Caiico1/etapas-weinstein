"""Ajustes comunes: los tests nunca consultan fuentes de noticias reales."""
import pytest

from etapas import rutina
from etapas.contexto import MarketContext


@pytest.fixture(autouse=True)
def _sin_red_de_noticias(monkeypatch):
    monkeypatch.setattr(rutina, "load_context", lambda now=None: MarketContext())
