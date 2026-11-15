"""Onboarding (box ③): profile fields from the person's answers or a document; the API routes. No AWS."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.service import ApiService, NoDispatcher
from app.intake import MockBackend
from app.intake.onboarding import ASKED, OnboardingDraft, document_text, draft_profile
from app.intake.schemas import AgentToggleChange, ProfileDraft

TEXT = ("Notes from my meeting with an advisor. I'm a student and I want to learn. Moderate risk. "
        "No crypto please. I don't care about the news. Also: buy milk and eggs.")


def docx(paragraphs):
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>' + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
           + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def pdf(text):
    """A one-page PDF with `text`, written by hand (correct xref offsets)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offsets))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref))
    return out.getvalue()


# ---------- Documents ----------

def test_word_and_pdf_and_text_files_are_read():
    assert "moderate risk" in document_text("me.docx", docx(["I want moderate risk", "for ten years"])).lower()
    assert "Moderate risk for my savings" in document_text("me.pdf", pdf("Moderate risk for my savings please"))
    assert document_text("me.txt", b"I want moderate risk over ten years.").startswith("I want")


@pytest.mark.parametrize("name, data, why", [
    ("me.png", b"\x89PNG....", "PDF, a Word"),
    ("me.docx", b"PK not really a zip", "Word file could not be read"),
    ("me.pdf", pdf("hi"), "No readable text"),
    ("big.txt", b"x" * (5 * 1024 * 1024 + 1), "larger than 5 MB"),
])
def test_unreadable_files_say_why(name, data, why):
    with pytest.raises(ValueError, match=why):
        document_text(name, data)


# ---------- Text → fields ----------

def test_found_and_missing_follow_the_chat_order():
    r = draft_profile(TEXT, MockBackend())
    assert r.fields["risk_band"] == "moderate" and r.fields["wants_to_learn"] is True
    assert r.fields["exclusions"] == [{"type": "asset_class", "value": "crypto", "min_severity": None}]
    assert r.fields["agent_toggles"] == {"news": False} and r.fields["language"] == "en"
    assert r.missing == [k for k in ASKED if k not in r.found] and "experience" in r.missing


def test_irrelevant_parts_are_reported_and_invented_quotes_dropped():
    class Model:
        name = "bedrock"

        def extract_profile(self, text):
            return OnboardingDraft(
                profile=ProfileDraft(risk_band="moderate", learning="unsaid", agent_toggle_changes=[
                    AgentToggleChange(agent="news", enabled=False, quote="I don't care about the news"),
                    AgentToggleChange(agent="esg", enabled=False, quote="ESG is useless")]),   # not in the text
                summary="Notes from a meeting with an advisor.", irrelevant=["a grocery list"])

    r = draft_profile(TEXT, Model())
    assert r.irrelevant == ["a grocery list"] and r.summary.startswith("Notes")
    assert r.fields["agent_toggles"] == {"news": False} and any("esg" in w for w in r.warnings)


# ---------- API ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AF_AUTH_MODE", "dev")
    return TestClient(create_app(ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher())))


def test_profile_draft_routes_read_text_and_files_and_save_nothing(client):
    h = {"X-Actor-Id": "newbie"}
    r = client.post("/v1/profile/draft", json={"text": TEXT}, headers=h)
    assert r.status_code == 200 and r.json()["fields"]["risk_band"] == "moderate"
    f = client.post("/v1/profile/draft/file", headers=h,
                    files={"file": ("notes.docx", docx(["Moderate risk, advanced investor, 10 years."]), "application/octet-stream")})
    assert f.status_code == 200 and f.json()["filename"] == "notes.docx" and f.json()["fields"]["experience"] == "advanced"
    assert client.get("/v1/profile", headers=h).status_code == 404            # nothing saved until the person saves


def test_profile_draft_errors_are_clear(client, monkeypatch):
    h = {"X-Actor-Id": "newbie"}
    assert client.post("/v1/profile/draft/file", headers=h, files={"file": ("x.png", b"\x89PNG", "image/png")}).status_code == 422
    assert client.post("/v1/profile/draft", json={"text": "  "}, headers=h).status_code == 422

    class Expired:
        name = "bedrock"

        def extract_profile(self, text):
            raise PermissionError("The security token included in the request is expired")

    monkeypatch.setattr(ApiService, "_backend", lambda self: Expired())
    r = client.post("/v1/profile/draft", json={"text": TEXT}, headers=h)
    assert r.status_code == 502 and "expired" in r.json()["detail"]
