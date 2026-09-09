from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote

import httpx

from app.config import Settings


SUGGESTION_ADJUSTMENT_FIELDS = {
    "target_city", "target_region", "target_countries", "sectors", "excluded_sectors",
    "client_types", "organization_types", "business_models", "sales_models",
    "employee_ranges", "revenue_ranges", "technologies", "opportunity_signals",
    "decision_roles", "keywords", "lookalike_companies", "ideal_company_size",
    "minimum_budget", "exclusions", "preferences", "hiring_recency", "funding_recency",
    "require_marketing_department", "require_sales_team", "require_ad_investment",
    "require_active_linkedin", "require_updated_website", "require_identifiable_decision_maker",
    "exclude_current_clients", "exclude_contacted_companies", "exclude_competitors",
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 504})
RETRY_DELAYS_SECONDS = (1.0, 2.0)
logger = logging.getLogger(__name__)

BOOLEAN_ADJUSTMENT_FIELDS = {
    "require_marketing_department", "require_sales_team", "require_ad_investment",
    "require_active_linkedin", "require_updated_website", "require_identifiable_decision_maker",
    "exclude_current_clients", "exclude_contacted_companies", "exclude_competitors",
}
ARRAY_ADJUSTMENT_FIELDS = {
    "target_countries", "sectors", "excluded_sectors", "client_types", "organization_types",
    "business_models", "sales_models", "employee_ranges", "revenue_ranges", "technologies",
    "opportunity_signals", "decision_roles", "keywords", "lookalike_companies",
}


def _adjustment_schema() -> dict:
    properties = {}
    for field in sorted(SUGGESTION_ADJUSTMENT_FIELDS):
        if field in BOOLEAN_ADJUSTMENT_FIELDS:
            properties[field] = {"type": "BOOLEAN"}
        elif field in ARRAY_ADJUSTMENT_FIELDS:
            properties[field] = {"type": "ARRAY", "items": {"type": "STRING"}}
        else:
            properties[field] = {"type": "STRING"}
    return {"type": "OBJECT", "properties": properties}


class GeminiSuggestionsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        model: str = "",
        response_body: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.model = model
        self.response_body = response_body


class GeminiSuggestionsTimeout(GeminiSuggestionsError):
    pass


class GeminiSuggestionsResponseError(GeminiSuggestionsError):
    pass


def _request_structured_json(service: "GeminiCriteriaSuggestions", request_body: dict) -> dict:
    """Call the configured models with the shared retry/fallback policy."""
    models = tuple(dict.fromkeys((service.model, service.fallback_model)))
    response: httpx.Response | None = None
    last_error: Exception | None = None
    with httpx.Client(timeout=service.timeout) as client:
        for model_index, model in enumerate(models):
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + quote(model, safe="") + ":generateContent"
            for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
                try:
                    candidate = client.post(
                        url,
                        headers={"x-goog-api-key": service.api_key, "Content-Type": "application/json"},
                        json=request_body,
                    )
                except httpx.TimeoutException as exc:
                    last_error = exc
                    logger.warning(
                        "Intelligent assistance request timed out model=%s attempt=%s timeout=%ss",
                        model,
                        attempt + 1,
                        service.timeout,
                    )
                    if attempt < len(RETRY_DELAYS_SECONDS):
                        time.sleep(RETRY_DELAYS_SECONDS[attempt])
                        continue
                    raise GeminiSuggestionsTimeout("SUGGESTIONS_TIMEOUT", model=model) from exc
                except httpx.HTTPError as exc:
                    raise GeminiSuggestionsError("SUGGESTIONS_NETWORK_ERROR", model=model) from exc

                if 200 <= candidate.status_code < 300:
                    response = candidate
                    break

                response_body = candidate.text[:1500]
                last_error = GeminiSuggestionsError(
                    "SUGGESTIONS_PROVIDER_ERROR",
                    status_code=candidate.status_code,
                    model=model,
                    response_body=response_body,
                )
                logger.warning(
                    "Intelligent assistance provider rejected request model=%s status=%s attempt=%s body=%s",
                    model,
                    candidate.status_code,
                    attempt + 1,
                    response_body,
                )
                if candidate.status_code == 503 and model_index == 0 and len(models) > 1:
                    break
                retryable = candidate.status_code in RETRYABLE_STATUS_CODES or (
                    candidate.status_code == 503 and model_index > 0
                )
                if retryable and attempt < len(RETRY_DELAYS_SECONDS):
                    time.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                raise last_error
            if response is not None:
                break

    if response is None:
        if isinstance(last_error, GeminiSuggestionsError):
            raise last_error
        raise GeminiSuggestionsError("SUGGESTIONS_PROVIDER_ERROR")
    try:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GeminiSuggestionsResponseError("SUGGESTIONS_INVALID_RESPONSE") from exc
    if not isinstance(result, dict):
        raise GeminiSuggestionsResponseError("SUGGESTIONS_INVALID_RESPONSE")
    return result


class GeminiCriteriaSuggestions:
    def __init__(self, settings: Settings):
        self.api_key = settings.gemini_api_key.strip()
        self.model = settings.gemini_model.strip() or "gemini-3.5-flash"
        self.fallback_model = settings.gemini_fallback_model.strip() or "gemini-3.5-flash-lite"
        self.timeout = max(1.0, min(float(settings.gemini_timeout_seconds), 60.0))

    def suggest(self, *, source_profile: dict, leads: list[dict]) -> list[dict]:
        if not self.api_key:
            raise GeminiSuggestionsError("GEMINI_API_KEY_REQUIRED")
        prompt = {
            "task": (
                "Analiza los leads ya aislados para una productora cuando existan y, si todavía no hay leads, "
                "usa su formulario y las respuestas del cuestionario para preparar la primera búsqueda. "
                "Devuelve exactamente tres mejoras concretas de criterios de prospección. "
                "Cada propuesta debe incluir entre uno y cinco ajustes no vacíos, usando únicamente las claves permitidas. "
                "Cruza los resultados con los datos del formulario: empresa, web, actividad, servicios, objetivos, "
                "cliente ideal, referencias, capacidad, mercados y modelo B2B o B2C. Usa los enlaces corporativos "
                "públicos recibidos únicamente como contexto verificable; no afirmes haberlos visitado ni inventes "
                "contenido que no esté en la entrada. Prioriza criterios que puedan producir relaciones comerciales "
                "recurrentes y explica por qué cada propuesta se adapta a esa productora. No propongas contacto, "
                "mensajería ni acciones externas; usa solo patrones del conjunto recibido."
            ),
            "productora": source_profile,
            "leads": leads,
            "allowed_adjustment_fields": sorted(SUGGESTION_ADJUSTMENT_FIELDS),
        }
        request_body = {
            "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT", "required": ["suggestions"],
                    "properties": {"suggestions": {
                        "type": "ARRAY", "minItems": 3, "maxItems": 3,
                        "items": {"type": "OBJECT", "required": ["title", "reason", "adjustments"],
                                  "properties": {"title": {"type": "STRING"}, "reason": {"type": "STRING"}, "adjustments": _adjustment_schema()}},
                    }},
                },
            },
        }
        try:
            raw = _request_structured_json(self, request_body)["suggestions"]
        except KeyError as exc:
            raise GeminiSuggestionsResponseError("SUGGESTIONS_INVALID_RESPONSE") from exc
        if not isinstance(raw, list) or len(raw) != 3:
            raise GeminiSuggestionsResponseError("SUGGESTIONS_INVALID_COUNT")
        suggestions = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict) or not isinstance(item.get("adjustments"), dict):
                raise GeminiSuggestionsResponseError("SUGGESTIONS_INVALID_ITEM")
            title = str(item.get("title") or "").strip()[:120]
            reason = str(item.get("reason") or "").strip()[:600]
            adjustments = {key: value for key, value in item["adjustments"].items() if key in SUGGESTION_ADJUSTMENT_FIELDS}
            if not title or not reason or not adjustments:
                raise GeminiSuggestionsResponseError("SUGGESTIONS_INCOMPLETE_ITEM")
            suggestions.append({"id": f"suggestion-{index}", "title": title, "reason": reason, "adjustments": adjustments})
        return suggestions

    def questionnaire(self, *, source_profile: dict, question_count: int) -> list[dict]:
        if not self.api_key:
            raise GeminiSuggestionsError("GEMINI_API_KEY_REQUIRED")
        count = max(5, min(30, int(question_count)))
        prompt = {
            "task": (
                f"Crea exactamente {count} preguntas breves y sencillas para que una productora audiovisual "
                "afine su búsqueda de clientes potenciales. Parte de su formulario ya completado y no repitas "
                "datos que ya estén claros. Prioriza tipo de cliente (empresas, personas o ambos), B2B/B2C, "
                "sectores incluidos y excluidos, países y regiones, tamaño, presupuesto, decisores, señales de "
                "oportunidad, servicios recurrentes y exclusiones. Alterna preguntas de selección múltiple y "
                "respuesta escrita. Las opciones deben ser concretas, permitir varias selecciones y no pedir "
                "datos sensibles. Usa únicamente los campos de ajuste permitidos."
            ),
            "productora": source_profile,
            "allowed_adjustment_fields": sorted(SUGGESTION_ADJUSTMENT_FIELDS),
        }
        request_body = {
            "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "required": ["questions"],
                    "properties": {
                        "questions": {
                            "type": "ARRAY",
                            "minItems": count,
                            "maxItems": count,
                            "items": {
                                "type": "OBJECT",
                                "required": ["title", "help", "kind", "options", "adjustment_field"],
                                "properties": {
                                    "title": {"type": "STRING"},
                                    "help": {"type": "STRING"},
                                    "kind": {"type": "STRING", "enum": ["MULTIPLE_CHOICE", "TEXT"]},
                                    "options": {"type": "ARRAY", "items": {"type": "STRING"}},
                                    "adjustment_field": {"type": "STRING"},
                                },
                            },
                        }
                    },
                },
            },
        }
        try:
            raw = _request_structured_json(self, request_body)["questions"]
        except KeyError as exc:
            raise GeminiSuggestionsResponseError("QUESTIONNAIRE_INVALID_RESPONSE") from exc
        if not isinstance(raw, list) or len(raw) != count:
            raise GeminiSuggestionsResponseError("QUESTIONNAIRE_INVALID_COUNT")
        questions = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise GeminiSuggestionsResponseError("QUESTIONNAIRE_INVALID_ITEM")
            title = str(item.get("title") or "").strip()[:180]
            help_text = str(item.get("help") or "").strip()[:300]
            kind = str(item.get("kind") or "").strip().upper()
            field = str(item.get("adjustment_field") or "").strip()
            options = list(dict.fromkeys(
                str(option).strip()[:100] for option in (item.get("options") or []) if str(option).strip()
            ))[:8]
            if kind not in {"MULTIPLE_CHOICE", "TEXT"} or field not in SUGGESTION_ADJUSTMENT_FIELDS:
                raise GeminiSuggestionsResponseError("QUESTIONNAIRE_INVALID_ITEM")
            if not title or not help_text or (kind == "MULTIPLE_CHOICE" and len(options) < 2):
                raise GeminiSuggestionsResponseError("QUESTIONNAIRE_INCOMPLETE_ITEM")
            questions.append({
                "id": f"question-{index}",
                "title": title,
                "help": help_text,
                "kind": kind.lower(),
                "options": options if kind == "MULTIPLE_CHOICE" else [],
                "adjustment_field": field,
            })
        return questions
