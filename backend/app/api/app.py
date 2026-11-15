"""Box ② API (FastAPI): routes of docs/architecture.mmd, link L1 (frontend → API Gateway → Lambda).

    POST /v1/runs           {text, profile_form?, portfolio_ref?} → 200 question/message, or 202 {run_id}
    GET  /v1/runs/{run_id}  → the run (status, plan, result once box ⑤ writes it)
    GET  /v1/profile        → the stored profile
    PUT  /v1/profile        → save the profile form
    GET  /v1/personas       → the brief's three example profiles (Amira, Mateo, Robert) as profile forms
    GET  /health
    /                       → the frontend (box ①, frontend/), served locally when that folder exists

Identity (actor_id): the `sub` claim of the Cognito JWT, already verified by the API Gateway JWT authorizer.
Local development only (AF_AUTH_MODE=dev): the X-Actor-Id header. Never trust an actor_id from a body.

Lambda: `handler` (Mangum adapter). Local: `uvicorn app.api.app:app --app-dir backend --reload`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import Profile
from app.store.runs import RunRecord

from .service import ApiError, ApiService, MessageIn, MessageOut, example_personas, service_from_env

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


def actor_id_from(request: Request) -> str:
    event = request.scope.get("aws.event") or {}
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    if claims.get("sub"):
        return str(claims["sub"])
    if os.environ.get("AF_AUTH_MODE", "jwt").lower() == "dev" and request.headers.get("x-actor-id"):
        return request.headers["x-actor-id"]
    raise HTTPException(status_code=401, detail="Not authenticated.")


def create_app(service: Optional[ApiService] = None) -> FastAPI:
    api = FastAPI(title="AlphaForge API (box 2)", version="1.0",
                  description="Simulation only: nothing here is investment advice.")
    svc = service or service_from_env()

    @api.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/v1/runs", response_model=MessageOut, response_model_exclude_none=True)
    def post_run(body: MessageIn, actor_id: str = Depends(actor_id_from)) -> JSONResponse:
        status, out = svc.post_message(actor_id, body)
        return JSONResponse(status_code=status, content=out.model_dump(mode="json", exclude_none=True))

    @api.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, actor_id: str = Depends(actor_id_from)) -> RunRecord:
        return svc.get_run(actor_id, run_id)

    @api.get("/v1/profile", response_model=Profile)
    def get_profile(actor_id: str = Depends(actor_id_from)) -> Profile:
        return svc.get_profile(actor_id)

    @api.put("/v1/profile", response_model=Profile)
    def put_profile(form: dict[str, Any] = Body(...), actor_id: str = Depends(actor_id_from)) -> Profile:
        return svc.put_profile(actor_id, form)

    @api.get("/v1/personas")
    def personas() -> list[dict[str, Any]]:
        return example_personas()

    # Box ① served by the same process for local use (one command, same origin, no CORS). Mounted last so
    # the API routes above win. On AWS the frontend is static files in S3 + CloudFront instead.
    frontend = Path(os.environ.get("AF_FRONTEND_DIR", FRONTEND_DIR))
    if os.environ.get("AF_SERVE_FRONTEND", "1") != "0" and (frontend / "index.html").is_file():
        api.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    return api


app = create_app()
_mangum = None  # built on the first Lambda invocation, reused by the warm container


def handler(event: dict, context: Any) -> dict:
    """AWS Lambda entry point (API Gateway HTTP API → Mangum → FastAPI)."""
    global _mangum
    if _mangum is None:
        from mangum import Mangum

        _mangum = Mangum(app, lifespan="off")
    return _mangum(event, context)
