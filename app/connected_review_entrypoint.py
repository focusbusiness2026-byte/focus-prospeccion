"""Render entry point. Serve only readiness status until the copy is configured."""
import os


if os.environ.get("REVIEW_ACTIVE") == "1":
    from app.connected_review import app
else:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok", "review": "awaiting_configuration"}

    @app.get("/")
    @app.get("/portal")
    def unavailable():
        return JSONResponse({"detail": "Copia privada pendiente de configuración."}, status_code=503)
