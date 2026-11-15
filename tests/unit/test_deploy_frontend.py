"""scripts/deploy_frontend.py (box ① on S3 + CloudFront): what is published and how CloudFront is configured.
Pure functions only, no AWS."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_frontend", ROOT / "scripts" / "deploy_frontend.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

POLICIES = {"cache_optimized": "opt", "cache_disabled": "off", "all_viewer_except_host": "fwd",
            "security_headers": "sec"}


def test_only_the_static_site_is_published():
    keys = [k for k, _ in deploy.site_files()]
    assert keys[0] == "index.html" and "assets/app.js" in keys and "assets/app.css" in keys
    assert not [k for k in keys if k.endswith((".py", ".pyc", ".md")) or "config" in k]  # dashboard, README, secrets


@pytest.mark.parametrize("key, ctype, cache", [
    ("index.html", "text/html; charset=utf-8", "no-cache"),
    ("config.js", "application/javascript; charset=utf-8", "no-cache"),
    ("assets/app.css", "text/css; charset=utf-8", "public, max-age=300"),
    ("assets/favicon.svg", "image/svg+xml", "public, max-age=300"),
])
def test_content_types_and_caching(key, ctype, cache):
    assert deploy.content_type(key) == ctype and deploy.cache_control(key) == cache


def test_config_js_keeps_cognito_and_calls_the_api_on_the_same_origin():
    cognito = 'window.AF_CONFIG = {"auth": "cognito", "clientId": "abc"};'
    text = deploy.config_js(cognito)
    assert cognito in text and text.endswith('window.AF_API_BASE = "";\n')
    assert "does not run without sign-in" in deploy.config_js(None)


def test_api_origin_takes_the_stage_as_origin_path_and_refuses_http():
    o = deploy.api_origin("https://abc123.execute-api.us-east-1.amazonaws.com/prod/")
    assert o["DomainName"] == "abc123.execute-api.us-east-1.amazonaws.com" and o["OriginPath"] == "/prod"
    assert o["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert deploy.api_origin("abc123.execute-api.us-east-1.amazonaws.com")["OriginPath"] == ""
    with pytest.raises(ValueError):
        deploy.api_origin("http://abc123.execute-api.us-east-1.amazonaws.com")


def test_distribution_without_api_serves_the_private_bucket_over_https():
    cfg = deploy.distribution_config("b.s3.us-east-1.amazonaws.com", "oac-1", POLICIES, "ref")
    origin = cfg["Origins"]["Items"][0]
    assert cfg["Origins"]["Quantity"] == 1 and origin["OriginAccessControlId"] == "oac-1"
    assert cfg["CacheBehaviors"] == {"Quantity": 0} and cfg["DefaultRootObject"] == "index.html"
    d = cfg["DefaultCacheBehavior"]
    assert d["ViewerProtocolPolicy"] == "redirect-to-https" and d["ResponseHeadersPolicyId"] == "sec"
    assert cfg["Comment"] == deploy.COMMENT


def test_distribution_with_api_routes_v1_uncached_with_the_bearer_token():
    cfg = deploy.distribution_config("b.s3.us-east-1.amazonaws.com", "oac-1", POLICIES, "ref",
                                     api_url="https://abc.execute-api.us-east-1.amazonaws.com")
    (api,) = cfg["CacheBehaviors"]["Items"]
    assert api["PathPattern"] == "/v1/*" and api["TargetOriginId"] == "api"
    assert api["CachePolicyId"] == "off" and api["OriginRequestPolicyId"] == "fwd"  # Authorization header forwarded
    assert {"POST", "PUT", "OPTIONS"} <= set(api["AllowedMethods"]["Items"])
    assert [o["Id"] for o in cfg["Origins"]["Items"]] == ["site", "api"]


def test_bucket_is_readable_only_by_this_distribution():
    stmt = deploy.bucket_policy("b", "arn:aws:cloudfront::1:distribution/D1")["Statement"][0]
    assert stmt["Principal"] == {"Service": "cloudfront.amazonaws.com"} and stmt["Action"] == "s3:GetObject"
    assert stmt["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("distribution/D1")
