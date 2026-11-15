"""How the dashboard talks to box ② (the API). Same routes in both modes:

  AF_API_URL set   → HTTP to a running API (uvicorn locally, or API Gateway; X-Actor-Id in dev mode)
  AF_API_URL unset → the FastAPI app in-process (fastapi TestClient): no server to start for a demo

The store (memory / DynamoDB), the intake backend (mock / bedrock) and the dispatcher (inline or none)
come from the usual AF_* variables (see app.api.service.service_from_env).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))


class ApiClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 600):
        self.base_url = base_url or os.environ.get("AF_API_URL")
        if self.base_url:
            import httpx

            self.http = httpx.Client(base_url=self.base_url.rstrip("/"), timeout=timeout)
            self.mode = f"HTTP {self.base_url}"
        else:
            os.environ.setdefault("AF_AUTH_MODE", "dev")      # X-Actor-Id accepted (local only)
            os.environ.setdefault("AF_DISPATCH", "inline")    # run box ⑤ right after the run is queued
            try:  # Windows: system certificates for botocore
                import truststore

                truststore.inject_into_ssl()
            except ImportError:
                pass
            from fastapi.testclient import TestClient

            import app.core.config  # noqa: F401  loads the repo .env (AWS_*, AF_*) before any AWS client
            from app.api.app import create_app
            from app.api.dispatch import InlineDispatcher
            from app.api.service import service_from_env

            svc = service_from_env()
            if isinstance(svc.dispatcher, InlineDispatcher):
                svc.dispatcher.portfolio_for = demo_portfolio
            self.service = svc
            self.http = TestClient(create_app(svc))
            self.mode = "in-process API"

    def _req(self, method: str, path: str, actor: str, **kw) -> tuple[int, Any]:
        r = self.http.request(method, path, headers={"X-Actor-Id": actor}, **kw)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text}

    def get_profile(self, actor: str) -> tuple[int, Any]:
        return self._req("GET", "/v1/profile", actor)

    def put_profile(self, actor: str, form: dict) -> tuple[int, Any]:
        return self._req("PUT", "/v1/profile", actor, json=form)

    def post_message(self, actor: str, text: str, profile_form: Optional[dict] = None) -> tuple[int, Any]:
        body: dict[str, Any] = {"text": text}
        if profile_form:
            body["profile_form"] = profile_form
        return self._req("POST", "/v1/runs", actor, json=body)

    def list_runs(self, actor: str) -> tuple[int, Any]:
        return self._req("GET", "/v1/runs", actor)

    def get_run(self, actor: str, run_id: str) -> tuple[int, Any]:
        return self._req("GET", f"/v1/runs/{run_id}", actor)

    def get_replay(self, actor: str, run_id: str) -> tuple[int, Any]:
        return self._req("GET", f"/v1/runs/{run_id}/replay", actor)

    def get_evidence(self, actor: str, run_id: str) -> tuple[int, Any]:
        return self._req("GET", f"/v1/runs/{run_id}/evidence", actor)


def persona_form(persona: dict) -> dict:
    """The persona file (core/personas.json) as a profile-form payload."""
    h = persona.get("horizon_years", [None, None])
    form = {k: persona.get(k) for k in ("risk_band", "experience", "language", "objective", "loss_capacity")}
    form["horizon_years"] = {"min": h[0], "max": h[1]}
    form.update({k: persona.get(k) for k in ("preferences", "constraints", "exclusions")})
    form["capital"] = (persona.get("simulated_capital") or {}).get("value")
    return {k: v for k, v in form.items() if v is not None}


DEMO_ACTOR_PREFIX = "demo-"


def personas() -> dict[str, dict]:
    import json

    raw = json.loads((ROOT / "backend" / "app" / "core" / "personas.json").read_text(encoding="utf-8"))
    return {p["id"]: p for p in raw["personas"]}


def demo_portfolio(actor_id: str) -> Optional[dict[str, float]]:
    """Current portfolio of a demo persona ("demo-robert" -> personas.json demo_request), else None."""
    if not actor_id.startswith(DEMO_ACTOR_PREFIX):
        return None
    p = personas().get(actor_id[len(DEMO_ACTOR_PREFIX):]) or {}
    pf = (p.get("demo_request") or {}).get("simulated_current_portfolio")
    return {k: float(v) for k, v in pf.items()} if pf else None
