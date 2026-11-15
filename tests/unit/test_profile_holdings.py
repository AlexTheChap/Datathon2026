"""Profile.holdings: what the user says they hold (web app, "I already have a portfolio")."""
import pytest
from app.api.service import ApiError, ApiService, NoDispatcher
from app.intake import MockBackend
from app.intake.agent import _known_profile
from app.store.profiles import InMemoryProfileStore


def _svc():
    return ApiService(intake_backend=MockBackend(), dispatcher=NoDispatcher(), profiles=InMemoryProfileStore())


def test_holdings_saved_with_the_chosen_marker():
    svc = _svc()
    svc.put_profile("ana", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    p = svc.put_profile("ana", {"holdings": {"XIU.TO": 0.6, "XBB.TO": 0.4}, "portfolio": {"XIU.TO": 0.6, "XBB.TO": 0.4},
                                "portfolio_run_id": "chosen:"})
    assert p.holdings == {"XIU.TO": 0.6, "XBB.TO": 0.4} and p.portfolio_run_id == "chosen:"
    assert svc.put_profile("ana", {"risk_band": "low"}).holdings == p.holdings  # kept by later saves


def test_holdings_weights_are_checked():
    svc = _svc()
    svc.put_profile("ana", {"risk_band": "moderate", "experience": "beginner", "language": "en"})
    with pytest.raises(ApiError):
        svc.put_profile("ana", {"holdings": {"XIU.TO": 1.5}})


def test_holdings_never_sent_to_the_intake_model():
    svc = _svc()
    stored = svc.put_profile("ana", {"risk_band": "moderate", "experience": "beginner", "language": "en", "holdings": {"XIU.TO": 1.0}})
    assert "holdings" not in _known_profile(stored, None)
