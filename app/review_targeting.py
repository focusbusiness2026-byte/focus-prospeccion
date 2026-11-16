"""Questionnaire provider for the offline review with synthetic accounts."""
from app.prospecting_guide import base_criteria


# Use the onboarding values first, then offer ways to narrow or expand them.
QUESTIONS = (
    ("sectors", "¿En qué sector quieres buscar primero?", ("Tecnología", "Servicios profesionales", "Comercio")),
    ("target_countries", "¿En qué país quieres buscar?", ("España", "Portugal", "Francia")),
    ("client_types", "¿Qué tipo de cliente encaja con tu oferta?", ("Empresa privada B2B", "Agencia", "Asociación")),
    ("ideal_company_size", "¿Qué tamaño de empresa prefieres?", ("11–50 empleados", "51–200 empleados", "Más de 200 empleados")),
    ("target_city", "¿Quieres priorizar una ciudad?", ("Sin preferencia", "Madrid", "Barcelona", "Valencia")),
    ("target_region", "¿Qué región quieres priorizar?", ("Sin preferencia", "Comunidad de Madrid", "Cataluña", "Comunidad Valenciana")),
    ("minimum_budget", "¿Qué presupuesto mínimo tiene sentido para tu oferta?", ("Sin preferencia", "500 €", "1.500 €", "3.000 €")),
    ("decision_roles", "¿Con quién te interesa hablar?", ("Dirección de marketing", "Dirección general", "Comunicación")),
    ("keywords", "¿Qué señal indica una oportunidad para tu empresa?", ("Sin preferencia", "Lanzamiento", "Expansión", "Nueva campaña")),
    ("exclusions", "¿Qué tipo de empresa quieres excluir?", ("Sin preferencia", "Competidores directos", "Empresas sin actividad reciente")),
    ("preferences", "¿Qué aspecto quieres priorizar en el resultado?", ("Afinidad con la oferta", "Momento de compra", "Cercanía")),
    ("decision_roles", "¿Hay otro equipo que participe en la decisión?", ("Sin preferencia", "Compras", "Dirección de marca")),
    ("keywords", "¿Qué otra señal te gustaría considerar?", ("Sin preferencia", "Nueva contratación", "Rebranding")),
    ("excluded_sectors", "¿Hay algún sector que no quieras incluir?", ("Sin preferencia", "Administración pública", "Hostelería")),
    ("preferences", "¿Cómo prefieres ordenar los resultados?", ("Por afinidad", "Por ubicación", "Por señales de compra")),
    ("client_types", "¿Quieres considerar otro tipo de empresa?", ("Sin preferencia", "Agencia", "Empresa privada B2B")),
    ("decision_roles", "¿Qué otro cargo sería relevante?", ("Sin preferencia", "Fundador", "Responsable de contenidos")),
    ("keywords", "¿Qué actividad reciente buscarías como pista?", ("Sin preferencia", "Feria comercial", "Apertura de oficina")),
    ("exclusions", "¿Hay alguna exclusión adicional?", ("Sin preferencia", "Empresas fuera de mi mercado", "Proveedores directos")),
    ("preferences", "¿Cuál es tu prioridad final para esta búsqueda?", ("Calidad de contactos", "Ajuste al servicio", "Ubicación")),
)


def review_turn(context):
    profile = context["profile"]
    criteria = base_criteria(profile)
    for index, ((field, _, _), entry) in enumerate(zip(QUESTIONS, context["history"])):
        answer = entry["answer"]
        if answer in {"Sin preferencia", "No lo sé todavía"}:
            continue
        if isinstance(criteria[field], list):
            if index < 3:
                criteria[field] = [answer[:500]]
            else:
                limit = 15 if field == "keywords" else 12
                criteria[field] = list(dict.fromkeys([*criteria[field], answer[:500]]))[:limit]
        else:
            limit = {"target_city": 200, "target_region": 200, "minimum_budget": 200, "exclusions": 1500}.get(field, 2000)
            criteria[field] = answer[:limit]
    extra = context.get("additional_context")
    if extra:
        criteria["preferences"] = " · ".join(filter(None, (criteria["preferences"], extra)))[:2000]

    question = ""
    options = []
    number = context["question_number"]
    if number:
        field, question, defaults = QUESTIONS[number - 1]
        current = criteria[field]
        candidates = current if isinstance(current, list) else [current]
        for value in (*candidates, *defaults):
            option = value[:65].strip()
            if option and all(option.casefold() != other.casefold() for other in options):
                options.append(option)
            if len(options) == 4:
                break

    sectors = ", ".join(criteria["sectors"]) or "sector por definir"
    countries = ", ".join(criteria["target_countries"]) or "mercado por definir"
    name = profile["productora"]
    summary = f"{name} enfocará la búsqueda en {sectors}, con prioridad en {countries}."
    return {
        "turn": {"summary": summary[:450], "question": question, "options": options,
                 "criteria": criteria, "assumptions": []},
        "sources": [], "web_status": "not_requested",
    }
