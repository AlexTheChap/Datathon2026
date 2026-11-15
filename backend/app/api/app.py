"""Box ② API (FastAPI): routes of docs/architecture.mmd, link L1 (frontend → API Gateway → Lambda).

    POST /v1/runs           {text, profile_form?, portfolio_ref?} → 200 question/message, or 202 {run_id}
    GET  /v1/runs/{run_id}  → the run (status, plan, result once box ⑤ writes it, incl. the Explainer's answer)
    GET  /v1/runs/{run_id}/evidence → the run's evidence catalogue (each item: id, tool, say, period, sources)
    GET  /v1/profile        → the stored profile
    PUT  /v1/profile        → save the profile form
    GET  /v1/personas       → the brief's three example profiles (Amira, Mateo, Robert) as profile forms
    GET  /v1/me             → who is signed in (actor_id, email, demo persona)
    GET  /health
    /                       → the frontend (box ①, frontend/), served locally when that folder exists

Identity (actor_id): the `sub` of a verified Amazon Cognito ID token (see auth.py: API Gateway authorizer
claims, or AF_AUTH_MODE=cognito to verify the Bearer token here). Local tests only (AF_AUTH_MODE=dev): the
X-Actor-Id header. Never trust an actor_id from a body. A demo account (amira/mateo/robert.demo@alphaforge.example)
gets its persona's profile on its first request.

Lambda: `handler` (Mangum adapter). Local: `uvicorn app.api.app:app --app-dir backend --reload`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.schemas import Profile
from app.store.runs import RunRecord

from .auth import AuthError, Identity, identify

from .service import ApiError, ApiService, MessageIn, MessageOut, example_personas, service_from_env

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


def identity_from(request: Request) -> Identity:
    try:
        return identify(request.scope.get("aws.event"), request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"}) from e


def auth_mode() -> str:
    return os.environ.get("AF_AUTH_MODE", "jwt").lower()


def log_auth_mode() -> None:
    """One line at startup saying how callers are identified (the first thing to check when sign-in fails)."""
    mode, log = auth_mode(), logging.getLogger("uvicorn.error")
    if mode == "cognito":
        pool = os.environ.get("AF_COGNITO_USER_POOL_ID") or "NOT SET: every request will get 401"
        log.info("AlphaForge API sign-in: Amazon Cognito (user pool %s)", pool)
    elif mode == "dev":
        log.info("AlphaForge API sign-in: dev mode, X-Actor-Id header, no password (local tests only)")
    else:
        log.info("AlphaForge API sign-in: API Gateway authorizer only; direct local requests get 401 "
                 "(start with AF_AUTH_MODE=cognito or dev)")


def actor_id_from(request: Request) -> str:
    return identity_from(request).actor_id


def create_app(service: Optional[ApiService] = None) -> FastAPI:
    api = FastAPI(title="AlphaForge API (box 2)", version="1.0",
                  description="Simulation only: nothing here is investment advice.")
    svc = service or service_from_env()

    @api.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    def current_actor(request: Request) -> str:
        """The verified caller; a demo account gets its persona's profile the first time it shows up."""
        ident = identity_from(request)
        svc.ensure_demo_profile(ident.actor_id, ident.demo_persona)
        return ident.actor_id

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/v1/me")
    def me(request: Request) -> dict[str, Any]:
        ident = identity_from(request)
        svc.ensure_demo_profile(ident.actor_id, ident.demo_persona)
        return {"actor_id": ident.actor_id, "email": ident.email, "demo_persona": ident.demo_persona}

    @api.post("/v1/runs", response_model=MessageOut, response_model_exclude_none=True)
    def post_run(body: MessageIn, actor_id: str = Depends(current_actor)) -> JSONResponse:
        status, out = svc.post_message(actor_id, body)
        return JSONResponse(status_code=status, content=out.model_dump(mode="json", exclude_none=True))

    @api.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, actor_id: str = Depends(current_actor)) -> RunRecord:
        return svc.get_run(actor_id, run_id)

    @api.get("/v1/runs/{run_id}/evidence")
    def get_evidence(run_id: str, actor_id: str = Depends(current_actor)) -> list[dict[str, Any]]:
        return svc.get_evidence(actor_id, run_id)

    @api.get("/v1/profile", response_model=Profile)
    def get_profile(actor_id: str = Depends(current_actor)) -> Profile:
        return svc.get_profile(actor_id)

    @api.put("/v1/profile", response_model=Profile)
    def put_profile(form: dict[str, Any] = Body(...), actor_id: str = Depends(current_actor)) -> Profile:
        return svc.put_profile(actor_id, form)

    @api.get("/v1/personas")
    def personas() -> list[dict[str, Any]]:
        return example_personas()

    # Box ① served by the same process for local use (one command, same origin, no CORS). Mounted last so
    # the API routes above win. On AWS the frontend is static files in S3 + CloudFront instead.
    frontend = Path(os.environ.get("AF_FRONTEND_DIR", FRONTEND_DIR))
    if os.environ.get("AF_SERVE_FRONTEND", "1") != "0" and (frontend / "index.html").is_file():

        @api.get("/config.js", include_in_schema=False)
        def frontend_config() -> Response:
            """The page's sign-in settings always match how this API checks callers: the Cognito config only when
            this API verifies Cognito tokens, otherwise none (the page then runs in dev mode)."""
            cfg = frontend / "config.js"
            if auth_mode() == "cognito" and cfg.is_file():
                body = cfg.read_text(encoding="utf-8")
            else:
                body = "// Sign-in is off: this API does not verify Cognito tokens (AF_AUTH_MODE is not cognito).\n"
            return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-store"})

        api.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")

    log_auth_mode()

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
