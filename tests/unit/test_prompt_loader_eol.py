"""Prompt templates must load and hash identically whatever the OS line endings
(Windows checkouts with core.autocrlf=true produce CRLF files)."""
from app.context.prompt_loader import PROMPTS_DIR, LocalPromptSource, resolve_explainer_prompt


def test_crlf_and_bom_templates_hash_like_lf(tmp_path, make_profile):
    lf_dir, crlf_dir = tmp_path / "lf", tmp_path / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()
    for f in PROMPTS_DIR.glob("explainer.*.md"):
        text = f.read_bytes().decode("utf-8").replace("\r\n", "\n")
        (lf_dir / f.name).write_bytes(text.encode("utf-8"))
        (crlf_dir / f.name).write_bytes(("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))

    profile = make_profile("robert")
    ref_lf = resolve_explainer_prompt(profile, LocalPromptSource(lf_dir))
    ref_crlf = resolve_explainer_prompt(profile, LocalPromptSource(crlf_dir))
    assert ref_crlf == ref_lf
