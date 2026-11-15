"""Client side of the AgentCore Gateway (link L12: sub-agents → Gateway, MCP over HTTPS, SigV4).

Two ways to reach the quant tools published by scripts/deploy_quant_gateway.py:

1. `GatewayClient`: plain JSON-RPC (tools/list, tools/call). No agent framework; used by the deploy
   script's --test and handy for debugging.
2. `strands_mcp_client()`: a Strands `MCPClient`, so a (sub-)agent gets the Gateway tools instead of
   the local Python functions:

       with strands_mcp_client() as mcp:
           agent = Agent(model=..., tools=mcp.list_tools_sync())

The Gateway uses the AWS_IAM authorizer: every HTTP request is signed with SigV4 (service
"bedrock-agentcore") using the caller's AWS credentials (IAM role on AgentCore Runtime, .env locally).
URL: AF_GATEWAY_URL (printed by the deploy script).
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

SERVICE = "bedrock-agentcore"


class SigV4Auth(httpx.Auth):
    """httpx auth that signs each request with SigV4 (botocore), body included."""

    requires_request_body = True

    def __init__(self, region: Optional[str] = None, session=None):
        import boto3

        self.session = session or boto3.Session()
        self.region = region or os.environ.get("AWS_REGION") or self.session.region_name or "us-east-1"

    def auth_flow(self, request: httpx.Request):
        from botocore.auth import SigV4Auth as _BotoSigV4
        from botocore.awsrequest import AWSRequest

        creds = self.session.get_credentials()
        if creds is None:
            raise RuntimeError("No AWS credentials: set AWS_* (or .env) or run with an IAM role.")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("connection", "content-length")}
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        _BotoSigV4(creds.get_frozen_credentials(), SERVICE, self.region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request


def gateway_url(url: Optional[str] = None) -> str:
    url = url or os.environ.get("AF_GATEWAY_URL")
    if not url:
        raise RuntimeError("AF_GATEWAY_URL is not set (run scripts/deploy_quant_gateway.py).")
    return url


def _parse_rpc(resp: httpx.Response) -> dict:
    """The Gateway answers JSON, or Server-Sent Events (one `data:` line per message)."""
    resp.raise_for_status()
    if "text/event-stream" in resp.headers.get("content-type", ""):
        msgs = [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]
        return msgs[-1]
    return resp.json()


class GatewayClient:
    """Minimal synchronous MCP client (JSON-RPC 2.0 over streamable HTTP)."""

    def __init__(self, url: Optional[str] = None, region: Optional[str] = None, session=None, timeout: float = 60):
        self.url = gateway_url(url)
        self.http = httpx.Client(auth=SigV4Auth(region, session), timeout=timeout)
        self._id = 0
        self._initialized = False

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        resp = self.http.post(self.url, json=body,
                              headers={"Accept": "application/json, text/event-stream",
                                       "Content-Type": "application/json"})
        msg = _parse_rpc(resp)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def initialize(self) -> dict:
        res = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "alphaforge", "version": "0.1"}})
        self._initialized = True
        return res

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.initialize()
        tools, cursor = [], None
        while True:
            res = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        """Returns the tool's JSON payload (the evidence envelope), decoded from the MCP text content."""
        if not self._initialized:
            self.initialize()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        payload: Any = texts[0] if len(texts) == 1 else texts
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
        if res.get("isError"):
            return {"error": payload}
        return payload


def strands_mcp_client(url: Optional[str] = None, region: Optional[str] = None, **kwargs):
    """Strands MCPClient pointed at the Gateway (tools named "<target>___<tool>")."""
    from strands.tools.mcp import MCPClient

    return MCPClient(url=gateway_url(url), auth_provider=SigV4Auth(region), **kwargs)
