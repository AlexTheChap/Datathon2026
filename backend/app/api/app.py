"""Box ② API (FastAPI): routes of docs/architecture.mmd, link L1 (frontend → API Gateway → Lambda).

    POST /v1/runs           {text, profile_form?, portfolio_ref?} → 200 question/message, or 202 {run_id}
    GET  /v1/runs           → the user's runs, newest first (dashboard history)
    GET  /v1/runs/{run_id}  → the run (status, plan, result once box ⑤ writes it, incl. the Explainer's answer)
    GET  /v1/runs/{run_id}/evidence → the run's evidence ledger (every tool call: inputs, output, source)
    GET  /v1/runs/{run_id}/evidence/catalog → what the answer can cite, as the page shows it (id, plain
                            title, say, readable period and sources, assumptions); profile-rule verdicts left out
    GET  /v1/runs/{run_id}/replay   → the run's portfolios replayed day by day over the 2022 crisis (dashboard)
    GET  /v1/profile        → the stored profile
    POST /v1/profile/draft[/file] → profile fields read from the person's text or a PDF / Word file (onboarding)
    PUT  /v1/profile        → save the profile form
    GET  /v1/personas       → the brief's three example profiles (Amira, Mateo, Robert) as profile forms
    GET  /v1/notifications → the caller's notification choice (opt_in, channel sms|email, phone, email, alerts)
    PUT  /v1/notifications {opt_in, channel?, phone?, email?, alerts?} → save it (opting out deletes the addresses)
    DELETE /v1/account     → erase the caller's data (profile, notification choice, analyses); the page then deletes
                            the sign-in account itself (Cognito DeleteUser)
    GET  /v1/notifications/sms → the caller's text-alert choice (opt_in, phone)
    PUT  /v1/notifications/sms {opt_in, phone?} → save it (opting out deletes the phone number)
    POST /v1/projection {weights?, start_value?, horizon_years?, monthly_contribution?, n_paths?} → range of
                            possible values under the 2026 planning guidelines (percentiles only, n_paths ≤ 20000;
                            defaults: saved portfolio, simulated capital, profile horizon)
    POST /v1/fit-score {weights?} → how well the weights fit the caller's profile, out of 100, every point explained (no LLM)
    POST /v1/fit-score/improve {weights?} → the closest mix that fits the profile better, each change explained (no LLM)
    POST /v1/rate-exposure {weights?} → how the holdings reacted to interest rates + the current rates (no LLM)
    GET  /v1/vol-forecast?tickers=A,B → GARCH volatility forecast vs last 3 months: rising | stable | easing
    GET  /v1/me             → who is signed in (actor_id, email, demo persona)
    GET  /health
    /                       → the frontend (box ①, frontend/), served locally when that folder exists

Identity (actor_id): the `sub` of a verified Amazon Cognito ID token (see auth.py: API Gateway authorizer
claims on Lambda; everywhere else, by default, the Bearer token is verified here against the pool in
backend/app/core/signin.json). Local tests only (AF_AUTH_MODE=dev, never a default): the X-Actor-Id header. Never trust an actor_id from a body. A demo account (amira/mateo/robert.demo@alphaforge.example)
gets its persona's profile on its first request.

Lambda: `handler` (Mangum adapter). Local: `uvicorn app.api.app:app --app-dir backend --reload`.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.notify.contacts import NotifyPrefsIn, SmsPrefsIn
from app.schemas import Profile
from app.store.runs import RunRecord

from .auth import AuthError, Identity, identify

from .service import (
    ApiError,
    ApiService,
    MessageIn,
    MessageOut,
    ProjectionIn,
    RateExposureIn,
    FitScoreIn,
    RunSummary,
    example_personas,
    service_from_env,
)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


class _FreshStaticFiles(StaticFiles):
    """The web app's files with "Cache-Control: no-cache": the browser revalidates them (ETag, a 304 when unchanged)
    on every load, so a new version of app.js or its modules is never mixed with an old cached one."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def identity_from(request: Request) -> Identity:
    try:
        return identify(request.scope.get("aws.event"), request.headers)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"}) from e


def auth_mode() -> str:
    from .auth import auth_mode as _mode

    return _mode()


def log_auth_mode() -> None:
    """One line at startup saying how callers are identified (the first thing to check when sign-in fails)."""
    mode, log = auth_mode(), logging.getLogger("uvicorn.error")
    if mode == "cognito":
        from .auth import signin_settings

        pool = signin_settings()["user_pool_id"] or "NOT SET (backend/app/core/signin.json): every request will get 401"
        log.info("AlphaForge API sign-in: Amazon Cognito (user pool %s)", pool)
    elif mode == "dev":
        log.info("AlphaForge API sign-in: dev mode, X-Actor-Id header, no password (local tests only)")
    else:
        log.info("AlphaForge API sign-in: API Gateway authorizer only; direct local requests get 401 "
                 "(start with AF_AUTH_MODE=cognito or dev)")


def actor_id_from(request: Request) -> str:
    return identity_from(request).actor_id


def create_app(service: Optional[ApiService] = None) -> FastAPI:
    from app.core.config import get_settings

    get_settings()   # loads the repo .env first: AWS_* must exist before Bedrock clients sign requests
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

    @api.get("/v1/runs", response_model=list[RunSummary])
    def list_runs(limit: int = 50, actor_id: str = Depends(actor_id_from)) -> list[RunSummary]:
        return svc.list_runs(actor_id, max(1, min(limit, 200)))

    @api.get("/v1/runs/{run_id}/replay")
    def get_replay(run_id: str, actor_id: str = Depends(actor_id_from)) -> dict[str, Any]:
        return svc.get_replay(actor_id, run_id)

    @api.get("/v1/runs/{run_id}/evidence")
    def get_evidence(run_id: str, actor_id: str = Depends(actor_id_from)) -> list[dict[str, Any]]:
        return svc.get_evidence(actor_id, run_id)

    @api.get("/v1/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str, actor_id: str = Depends(current_actor)) -> RunRecord:
        return svc.get_run(actor_id, run_id)

    @api.get("/v1/runs/{run_id}/evidence/catalog")
    def get_evidence_catalog(run_id: str, actor_id: str = Depends(current_actor)) -> list[dict[str, Any]]:
        return svc.get_evidence_catalog(actor_id, run_id)

    @api.get("/v1/profile", response_model=Profile)
    def get_profile(actor_id: str = Depends(current_actor)) -> Profile:
        return svc.get_profile(actor_id)

    @api.put("/v1/profile", response_model=Profile)
    def put_profile(form: dict[str, Any] = Body(...), actor_id: str = Depends(current_actor)) -> Profile:
        return svc.put_profile(actor_id, form)

    # News alerts by text (app.notify): opt-in + phone, kept apart from the profile (never sent to a model).
    @api.get("/v1/notifications/sms")
    def get_sms(actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.get_sms(actor_id)

    @api.put("/v1/notifications/sms")
    def put_sms(body: SmsPrefsIn, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.put_sms(actor_id, body)

    # Every channel and alert type (sign-up "Inform me when…", My profile settings). Same item as above.
    @api.get("/v1/notifications")
    def get_notifications(actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.get_notifications(actor_id)

    @api.put("/v1/notifications")
    def put_notifications(body: NotifyPrefsIn, request: Request) -> dict[str, Any]:
        ident = identity_from(request)            # the account's email is the default address for email alerts
        svc.ensure_demo_profile(ident.actor_id, ident.demo_persona)
        return svc.put_notifications(ident.actor_id, body, ident.email)

    @api.delete("/v1/account")
    def delete_account(request: Request) -> dict[str, Any]:
        ident = identity_from(request)            # a shared demo account is refused (its password is public)
        return svc.delete_account(ident.actor_id, demo_persona=ident.demo_persona)

    # Onboarding (box ③): read the person's answers or a document, return the profile fields found. Saves nothing.
    @api.post("/v1/profile/draft")
    def profile_draft(body: dict[str, Any] = Body(...), actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.draft_profile(actor_id, str(body.get("text") or ""))

    @api.post("/v1/profile/draft/file")
    async def profile_draft_file(file: UploadFile = File(...), actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.draft_profile_file(actor_id, file.filename or "", await file.read())

    # Box ⑥ quant models (app.quant): a range of outcomes under the planning guidelines, the volatility trend.
    @api.post("/v1/projection")
    def projection(body: ProjectionIn, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.projection(actor_id, body)

    @api.post("/v1/rate-exposure")
    def rate_exposure(body: RateExposureIn, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.rate_exposure(actor_id, body)

    @api.post("/v1/fit-score")
    def fit_score(body: FitScoreIn, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.fit_score(actor_id, body)

    @api.post("/v1/fit-score/improve")
    def improve_fit(body: FitScoreIn, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.improve_fit(actor_id, body)

    @api.get("/v1/vol-forecast")
    def vol_forecast(tickers: str, actor_id: str = Depends(current_actor)) -> dict[str, Any]:
        return svc.vol_forecast(actor_id, tickers.split(","))

    @api.get("/v1/personas")
    def personas() -> list[dict[str, Any]]:
        return example_personas()

    # Box ① served by the same process for local use (one command, same origin, no CORS). Mounted last so
    # the API routes above win. On AWS the frontend is static files in S3 + CloudFront instead.
    frontend = Path(os.environ.get("AF_FRONTEND_DIR", FRONTEND_DIR))
    if os.environ.get("AF_SERVE_FRONTEND", "1") != "0" and (frontend / "index.html").is_file():

        @api.get("/config.js", include_in_schema=False)
        def frontend_config() -> Response:
            """The page's sign-in settings always match how this API checks callers: the Cognito config when this API
            verifies Cognito tokens; the no-password test mode only when this API runs with AF_AUTH_MODE=dev (local
            tests); otherwise none, and the page then refuses to run (no account, no app: never a guest user)."""
            from .auth import browser_config

            cfg = browser_config(os.environ.get("AF_DEMO_PASSWORD")) if auth_mode() == "cognito" else None
            if cfg:
                body = ("// Sign-in settings of this server (backend/app/core/signin.json).\n"
                        f"window.AF_CONFIG = {json.dumps(cfg, indent=2)};\n")
            elif auth_mode() == "dev":
                body = ("// Local test mode (AF_AUTH_MODE=dev): no password, X-Actor-Id header. Never for real users.\n"
                        'window.AF_CONFIG = {"auth": "dev"};\n')
            else:
                body = "// No sign-in settings on this server: the page does not run without an account.\n"
            return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-store"})

        api.mount("/", _FreshStaticFiles(directory=frontend, html=True), name="frontend")

    log_auth_mode()

    return api


app = create_app()
_mangum = None  # built on the first Lambda invocation, reused by the warm container


def handler(event: dict, context: Any) -> dict:
    """AWS Lambda entry point (API Gateway HTTP API or Function URL → Mangum → FastAPI).

    {"af_dispatch_run": run_id}: the asynchronous self-invocation of AF_DISPATCH=lambda (api/dispatcher.py):
    runs the orchestrator + Explainer for that QUEUED run and writes the outcome to the run store."""
    if isinstance(event, dict) and event.get("af_dispatch_run"):
        from .dispatch import InlineDispatcher

        runs, run_id = service_from_env().runs, str(event["af_dispatch_run"])
        run = runs.get(run_id)
        if run is not None and run.status == "QUEUED":   # an async retry never runs the same analysis twice
            InlineDispatcher(runs).dispatch(run)
            run = runs.get(run_id)
        return {"run_id": run_id, "status": run.status if run else "NOT_FOUND"}
    global _mangum
    if _mangum is None:
        from mangum import Mangum

        _mangum = Mangum(app, lifespan="off")
    return _mangum(event, context)
