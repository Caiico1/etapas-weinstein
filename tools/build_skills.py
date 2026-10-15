"""Empaqueta las habilidades pool-liquidez y pool-cribado para subirlas a claude.ai.

    python tools/build_skills.py

Copia los módulos del motor a out/<habilidad>/scripts/etapas, con los parches de la versión
empaquetada (solo Uniswap v3 y v4, sin Orca; textos «/pool» → «pool-liquidez»; órdenes con los
scripts de la habilidad en lugar de «python -m etapas...»), añade SKILL.md y los scripts de entrada
de tools/skills y genera out/pool-liquidez.zip y out/pool-cribado.zip. Cada parche se comprueba: si
el texto original ya no está, el build falla en lugar de empaquetar algo a medias.
"""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, SKILLS, OUT = ROOT / "etapas", ROOT / "tools" / "skills", ROOT / "out"
SKILL_NAMES = ("pool-liquidez", "pool-cribado")
MODULES = ("__init__", "analysis", "classifier", "config", "cribado", "data", "descubrir", "indicators",
           "keccak", "liquidez", "oportunidad", "pools", "seguimiento", "tema")

# (módulo, texto original, texto empaquetado)
PATCHES = [
    ("descubrir", 'FUENTES = ("uniswap_v3", "uniswap_v4", "orca")',
     'FUENTES = ("uniswap_v3", "uniswap_v4")      # para volver a incluir Orca (Solana), añade "orca" y\n'
     '#                                              "solana" en cribado.NETWORKS'),
    ("descubrir", "(Uniswap v3, Uniswap v4 sin hook y Orca)", "(por defecto, Uniswap v3 y v4 sin hook)"),
    ("cribado", 'NETWORKS = {"ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum", "solana": "Solana"}',
     'NETWORKS = {"ethereum": "Ethereum", "base": "Base", "arbitrum": "Arbitrum"}      # sin Orca (Solana)'),
    ("cribado", 'PROJECTS = {"uniswap-v3": "Uniswap v3", "uniswap-v4": "Uniswap v4", "orca-dex": "Orca"}',
     'PROJECTS = {"uniswap-v3": "Uniswap v3", "uniswap-v4": "Uniswap v4"}'),
    ("oportunidad", 'f"No hay pools de Uniswap v3, v4 ni de Orca para {base}/{quote} con los tokens verificados"',
     'f"No hay pools de Uniswap v3 ni v4 para {base}/{quote} con los tokens verificados "\n'
     '                            f"(esta habilidad solo busca en Uniswap v3 y v4)"'),
    ("oportunidad", "`python -m etapas.seguimiento ", "`python seguimiento.py "),
    ("oportunidad", 'prog="python -m etapas.oportunidad"', 'prog="python pool.py"'),
    ("cribado", 'prog="python -m etapas.cribado"', 'prog="python cribado.py"'),
    ("seguimiento", 'prog="python -m etapas.seguimiento"', 'prog="python seguimiento.py"'),
]
# Menciones a la orden /pool del proyecto (no a /pool-cribado ni a rutas)
SLASH_POOL = re.compile(r"/pool(?![-\w])")


def build_module(name: str) -> str:
    text = (SRC / f"{name}.py").read_text(encoding="utf-8")
    for module, old, new in PATCHES:
        if module == name:
            if old not in text:
                sys.exit(f"build_skills: el parche de {name}.py ya no encaja: {old!r}")
            text = text.replace(old, new)
    return SLASH_POOL.sub("pool-liquidez", text)


def build(skill: str) -> Path:
    """Escribe out/<skill>.zip directamente (sin carpeta intermedia)."""
    zip_path = OUT / f"{skill}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{skill}/SKILL.md", (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8"))
        for f in sorted((SKILLS / "scripts").glob("*.py")):
            z.writestr(f"{skill}/scripts/{f.name}", f.read_text(encoding="utf-8"))
        for name in MODULES:
            z.writestr(f"{skill}/scripts/etapas/{name}.py", build_module(name))
    return zip_path


if __name__ == "__main__":
    for s in SKILL_NAMES:
        print(build(s))
