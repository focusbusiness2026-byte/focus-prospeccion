from __future__ import annotations

import json
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


class GeminiSuggestionsError(RuntimeError):
    pass


class GeminiSuggestionsTimeout(GeminiSuggestionsError):
    pass


class GeminiSuggestionsResponseError(GeminiSuggestionsError):
    pass


class GeminiCriteriaSuggestions:
    def __init__(self, settings: Settings):
        self.api_key = settings.gemini_api_key.strip()
        self.model = settings.gemini_model.strip() or "gemini-2.5-flash"
        self.timeout = max(1.0, min(float(settings.gemini_timeout_seconds), 60.0))

    def suggest(self, *, source_profile: dict, leads: list[dict]) -> list[dict]:
        if not self.api_key:
            raise GeminiSuggestionsError("GEMINI_API_KEY_REQUIRED")
        prompt = {
            "task": (
                "Analiza exclusivamente estos leads ya aislados para una productora. "
                "Devuelve exactamente tres mejoras concretas de criterios de prospección. "
                "No inventes datos ni propongas acciones externas; usa solo patrones del conjunto recibido."
            ),
            "productora": source_profile,
            "leads": leads,
            "allowed_adjustment_fields": sorted(SUGGESTION_ADJUSTMENT_FIELDS),
        }
        url = "https://generativelanguage.googleapis.com/v1beta/models/" + quote(self.model, safe="") + ":generateContent"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
                        "generationConfig": {
                            "temperature": 0.2,
                            "responseMimeType": "application/json",
                            "responseSchema": {
                                "type": "OBJECT", "required": ["suggestions"],
                                "properties": {"suggestions": {
                                    "type": "ARRAY", "minItems": 3, "maxItems": 3,
                                    "items": {"type": "OBJECT", "required": ["title", "reason", "adjustments"],
                                              "properties": {"title": {"type": "STRING"}, "reason": {"type": "STRING"}, "adjustments": {"type": "OBJECT"}}},
                                }},
                            },
                        },
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise GeminiSuggestionsTimeout("Gemini no respondió dentro del tiempo permitido") from exc
        except httpx.HTTPError as exc:
            raise GeminiSuggestionsError("Gemini no está disponible temporalmente") from exc
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            raw = json.loads(text)["suggestions"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeminiSuggestionsResponseError("Gemini devolvió una respuesta no válida") from exc
        if not isinstance(raw, list) or len(raw) != 3:
            raise GeminiSuggestionsResponseError("Gemini debe devolver exactamente 3 sugerencias")
        suggestions = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict) or not isinstance(item.get("adjustments"), dict):
                raise GeminiSuggestionsResponseError("Gemini devolvió una sugerencia no válida")
            title = str(item.get("title") or "").strip()[:120]
            reason = str(item.get("reason") or "").strip()[:600]
            adjustments = {key: value for key, value in item["adjustments"].items() if key in SUGGESTION_ADJUSTMENT_FIELDS}
            if not title or not reason or not adjustments:
                raise GeminiSuggestionsResponseError("Gemini devolvió una sugerencia incompleta")
            suggestions.append({"id": f"suggestion-{index}", "title": title, "reason": reason, "adjustments": adjustments})
        return suggestions
