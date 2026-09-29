"""Equipo de agentes: cada uno aplica sus reglas a los datos y el coordinador junta sus informes.

Agentes (deterministas: reglas explícitas y auditables, sin coste):
  - Etapas: etapa de Weinstein por marco (classifier.py).
  - Rangos: mínimo y máximo por perfil, ajustados a los ticks de cada pool (liquidez.py).
  - Riesgo: idoneidad de cada perfil según la etapa y el marco superior (liquidez.verdict).
  - Contexto: sentimiento, Reserva Federal y titulares (contexto.py). No cambia nada: informa.
  - Vigilante: estado de las posiciones abiertas de posiciones.txt (posiciones.py).
  - Coordinador: el dictamen final en JSON.

El dictamen es la interfaz para una IA generativa, sin cambiar nada más:
  - Gratis: cada rutina deja out/ia/<SÍMBOLO>.md, listo para pegar en claude.ai y preguntar.
  - Con la API de Anthropic (de pago, opcional): `python -m etapas --rutina --ia` envía el mismo
    texto a Claude y guarda la respuesta en out/ia/<SÍMBOLO>_claude.md. La IA solo explica: todos
    los números salen de los agentes, nunca del modelo.
"""
import json
import os
from pathlib import Path

from .analysis import AssetResult
from .config import DISCLAIMER, ORDER
from .contexto import MarketContext, fng_history, headlines_for, next_fomc
from .liquidez import LiquidityResult

IA_MODEL = "claude-opus-5"

INSTRUCCIONES = """Eres un analista de liquidez concentrada en finanzas descentralizadas (Uniswap v3, Orca).
Recibes el dictamen de un equipo de agentes que ya ha calculado todo: etapa de Weinstein por marco
temporal, rangos de precio por perfil (diario, semanal y mensual), idoneidad de cada perfil,
estimación de comisiones y contexto de mercado.

Tu tarea:
1. Explica en español claro, para alguien que no es técnico, qué perfil encaja mejor ahora y por qué.
2. Para cada perfil, resume el rango (mín. y máx. ajustados a ticks del pool con más comisiones estimadas), qué
   pasa si el precio sale por arriba o por abajo y cuántos días de comisiones hacen falta para
   compensar la pérdida en el borde.
3. Señala los riesgos y lo que habría que vigilar (niveles que confirman o invalidan la etapa).
4. Si hay posiciones abiertas con aviso, di qué opciones hay (mantener, cerrar o reajustar el rango).

Reglas: usa SOLO los números del dictamen; no inventes precios ni probabilidades. Si falta un dato,
dilo. No es asesoramiento financiero: termina recordándolo en una frase."""


def dictamen(asset: AssetResult, liq: LiquidityResult | None, ctx: MarketContext | None = None,
             positions: list[dict] | None = None) -> dict:
    """Informe conjunto de los agentes para un activo."""
    etapas = {}
    for k in ORDER:
        r = asset.timeframes[k]
        if r.status != "ok":
            etapas[r.label] = {"estado": r.message or r.status}
            continue
        etapas[r.label] = {"etapa": r.stage, "nombre": r.stage_name, "transicion": r.transition or None,
                           "confianza": r.confidence_label, "cierre_referencia": r.price,
                           "vela": r.candle_time, "media": f"SMA{r.ma_len}", "pendiente": r.slope_label,
                           "estructura": r.structure, "nivel_confirma": r.confirm_level,
                           "nivel_invalida": r.invalid_level}
    contexto = {}
    if ctx is not None:
        if ctx.fng:
            contexto["miedo_codicia"] = {"hoy": ctx.fng["hoy"], "clase": ctx.fng["clase"]} | {
                label.replace(" ", "_"): {"valor": v, "clase": c} for label, v, c in fng_history(ctx.fng)}
        nxt = next_fomc(ctx)
        if nxt:
            contexto["proxima_fed"] = f"{nxt[0]:%Y-%m-%d} a {nxt[1]:%Y-%m-%d}"
        contexto["titulares"] = [h.title for h in headlines_for(ctx, asset.symbol, asset.kind)[:5]]
    d = {"activo": asset.symbol, "precio_actual": asset.last_price,
         "agentes": {"etapas": etapas, "contexto": contexto,
                     "posiciones": [p for p in (positions or []) if p["simbolo"] == asset.symbol]},
         "aviso": DISCLAIMER}
    if liq is not None:
        ld = liq.to_dict()
        d["agentes"]["rangos"] = ld["perfiles"]
        d["agentes"]["riesgo"] = {p["perfil"]: {"idoneidad": p["idoneidad"], "motivos": p["motivos"]}
                                  for p in ld["perfiles"]}
        d["coordinador"] = {"recomendacion": liq.best, "nota_pools": liq.note or None,
                            "capital_referencia_usd": liq.capital}
    return d


def prompt_text(d: dict) -> str:
    return (f"{INSTRUCCIONES}\n\n## Dictamen de los agentes ({d['activo']})\n\n```json\n"
            f"{json.dumps(d, ensure_ascii=False, indent=2, default=str)}\n```\n")


def write_prompt(d: dict, out_dir: Path) -> Path:
    folder = out_dir / "ia"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{d['activo']}.md"
    path.write_text(prompt_text(d), encoding="utf-8")
    return path


def ask_claude(d: dict, out_dir: Path) -> tuple[Path | None, str]:
    """Envía el dictamen a Claude (requiere `pip install anthropic` y credenciales de la API).
    Devuelve (archivo con la respuesta o None, mensaje)."""
    try:
        import anthropic
    except ImportError:
        return None, "falta el paquete anthropic (pip install anthropic)"
    client = anthropic.Anthropic()
    try:
        # fallbacks "default": si el modelo rechaza la petición, la API la repite con el modelo
        # de respaldo recomendado en la misma llamada.
        msg = client.beta.messages.create(
            model=IA_MODEL, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
            output_config={"effort": "medium"},
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": prompt_text(d, instructions=False)}],
        )
    except anthropic.AuthenticationError:
        return None, "credenciales de la API de Anthropic no válidas o ausentes (ANTHROPIC_API_KEY)"
    except anthropic.RateLimitError:
        return None, "límite de peticiones de la API alcanzado; prueba más tarde"
    except anthropic.APIStatusError as e:
        return None, f"error de la API ({e.status_code})"
    except anthropic.APIConnectionError:
        return None, "sin conexión con la API de Anthropic"
    if msg.stop_reason == "refusal":
        return None, "el modelo no respondió a esta petición"
    text = "".join(b.text for b in msg.content if b.type == "text")
    path = out_dir / "ia" / f"{d['activo']}_claude.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, "ok"


def ia_enabled() -> bool:
    return os.environ.get("ETAPAS_IA", "") == "1"
