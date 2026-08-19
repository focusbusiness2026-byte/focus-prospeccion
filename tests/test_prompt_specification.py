from pathlib import Path


SPEC = Path(__file__).parents[1] / "docs" / "PROMPT_MAESTRO_PROSPECCION.md"


def test_prompt_specification_covers_required_contract():
    text = SPEC.read_text(encoding="utf-8")

    required_fragments = (
        "Prospección y Focus Viral Radar son productos distintos",
        "PENDIENTE DE VALIDAR",
        "Prompt complementario del cliente",
        "dominio o web normalizado, señal principal",
        "existentes, nuevos, fusionables y pendientes de revisión",
        "No disponible públicamente",
        "Hasta 5 resultados con mayor ajuste a los filtros",
        "No llamar fuentes externas ni consumir cuota",
        "no se importa automáticamente a GoHighLevel",
        "URL de procedencia, fecha de consulta y criterio",
    )

    for fragment in required_fragments:
        assert fragment.casefold() in text.casefold(), fragment


def test_prompt_precedence_keeps_client_prompt_below_base_rules():
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## 4. Precedencia de instrucciones", 1)[1].split("## 5.", 1)[0]

    assert section.index("Prompt base de Focus Business") < section.index(
        "Prompt complementario del cliente"
    )
    assert "nunca puede anular" in text


def test_specification_is_local_only():
    text = SPEC.read_text(encoding="utf-8")

    assert "especificación local" in text
    assert "no activa consultas, cuotas, escrituras, importaciones ni despliegues" in text
    assert "Esta entrega solo añade documentación contractual" in text
