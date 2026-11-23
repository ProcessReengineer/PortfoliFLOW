# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service-level tests for ScraperService.

Stubs :meth:`AIServiceCore.send_one_shot_extraction` via monkeypatch so we can
control LLM responses and error injection deterministically. HTTP-layer
correctness is covered by tests/characterization/test_ai_service_core.py.

Since ADR-0123 the service takes a :class:`ResolvedLLM` rather than a model
id, and the singleton's connection state is irrelevant to it — the fixture
below deliberately does **not** configure the singleton, which is itself the
regression pin: a service that reached for the parked credentials again would
fail on an unconfigured core.

No Qt setup — ScraperService is PyQt-free.
"""

from __future__ import annotations

import json

import pytest

from services.ai_service_core import ResolvedLLM

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: The model every happy-path test resolves to — PDF-capable per the shipped
#: capability map.
_MODEL = "anthropic/claude-sonnet-4.5"


def _llm(model: str = _MODEL) -> ResolvedLLM:
    """Build one run's resolution, as the web surface would hand it over."""
    return ResolvedLLM(base_url="https://example.test/v1", api_key="sk-test", model=model)


@pytest.fixture
def stub_ai_service(monkeypatch):
    """Replace ``AIServiceCore.send_one_shot_extraction`` with a stub.

    Returns a dict with:
      - 'calls': list of dicts recording each invocation (messages, llm)
      - 'responses': list to populate; each entry is ('return', value) or
        ('raise', Exception). Consumed FIFO.
        RuntimeError if the queue runs dry mid-test.

    The singleton is left **unconfigured** on purpose (ADR-0123): the service
    passes ``llm=`` and must never consult the parked triple again.
    """
    calls: list[dict] = []
    responses: list[tuple[str, object]] = []

    def _fake_send(self, messages, model=None, *, llm=None, **kwargs):
        calls.append({"messages": messages, "model": model, "llm": llm, "kwargs": kwargs})
        if not responses:
            raise RuntimeError("Test stub exhausted — add more entries to responses")
        kind, value = responses.pop(0)
        if kind == "raise":
            raise value
        return value

    # ADR-0038 split: services/scraper/service.py imports the Qt-free
    # core, not the legacy shim. Patch the core's class method.
    from services import ai_service_core as ai_core_mod

    monkeypatch.setattr(
        ai_core_mod.AIServiceCore,
        "send_one_shot_extraction",
        _fake_send,
    )

    return {"calls": calls, "responses": responses}


@pytest.fixture
def two_keywords():
    from services.scraper.models import Keyword, KeywordType

    return [
        Keyword(name="NAV", type=KeywordType.NUMBER),
        Keyword(name="IRR", type=KeywordType.PERCENTAGE),
    ]


@pytest.fixture
def pdf_attachment():
    from services.scraper.models import Attachment

    return Attachment(
        filename="fund_q3_2024.pdf",
        mime_type="application/pdf",
        data=b"%PDF-1.4 dummy content",
    )


def _valid_response(fund_name: str = "Example Fund I", period: str = "Q3 2024") -> str:
    """Build a valid LLM-style response string."""
    payload = {
        "fund_name": fund_name,
        "period": period,
        "findings": {
            "NAV": {
                "value": "125000000",
                "source": "Page 4, NAV summary",
                "confidence": "High",
            },
            "IRR": {
                "value": "14.2%",
                "source": "Page 2, executive summary",
                "confidence": "Medium",
            },
        },
    }
    return "```json\n" + json.dumps(payload) + "\n```"


# ---------------------------------------------------------------------------
# TestLoadScraperPrompt
# ---------------------------------------------------------------------------


class TestLoadScraperPrompt:
    def test_loads_real_file_successfully(self) -> None:
        from services.scraper.service import load_scraper_prompt

        prompt = load_scraper_prompt()
        assert len(prompt) > 0
        assert "High" in prompt
        assert "fund_name" in prompt

    def test_raises_when_file_missing(self, tmp_path) -> None:
        from pathlib import Path

        from services.scraper.service import load_scraper_prompt

        with pytest.raises(FileNotFoundError):
            load_scraper_prompt(path=Path("/nonexistent/file.md"))

    def test_raises_on_missing_fence(self, tmp_path) -> None:
        from services.scraper.service import load_scraper_prompt

        f = tmp_path / "prompt.md"
        f.write_text("# No fences here\nJust prose.\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"No ``` fence"):
            load_scraper_prompt(path=f)

    def test_raises_on_unclosed_fence(self, tmp_path) -> None:
        from services.scraper.service import load_scraper_prompt

        f = tmp_path / "prompt.md"
        f.write_text("# Unclosed\n```\nsome content without closing\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Unclosed"):
            load_scraper_prompt(path=f)

    def test_raises_on_empty_fence(self, tmp_path) -> None:
        from services.scraper.service import load_scraper_prompt

        f = tmp_path / "prompt.md"
        f.write_text("```\n```\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Empty"):
            load_scraper_prompt(path=f)


# ---------------------------------------------------------------------------
# TestScraperServiceHappyPath
# ---------------------------------------------------------------------------


class TestScraperServiceHappyPath:
    def test_scrape_single_file_happy_path(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.models import Confidence
        from services.scraper.service import ScraperService

        stub_ai_service["responses"].append(("return", _valid_response()))

        svc = ScraperService()
        result = svc.scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert len(result.extractions) == 1
        assert result.cancelled is False
        ext = result.extractions[0]
        assert ext.filename == "fund_q3_2024.pdf"
        assert ext.fund_name == "Example Fund I"
        assert ext.period == "Q3 2024"
        assert ext.error is None
        assert len(ext.findings) == 2
        # findings in keyword input order
        assert ext.findings[0].keyword.name == "NAV"
        assert ext.findings[1].keyword.name == "IRR"
        assert ext.findings[0].value == "125000000"
        assert ext.findings[0].confidence == Confidence.HIGH
        assert ext.findings[1].confidence == Confidence.MEDIUM

    def test_stub_received_expected_llm_and_messages(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.service import ScraperService

        stub_ai_service["responses"].append(("return", _valid_response()))

        svc = ScraperService()
        svc.scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert len(stub_ai_service["calls"]) == 1
        call = stub_ai_service["calls"][0]
        # The resolution reaches the core as ``llm=`` — never as a bare model
        # id against the parked singleton (ADR-0123).
        assert call["model"] is None
        assert call["llm"] is not None
        assert call["llm"].model == _MODEL
        assert call["llm"].api_key == "sk-test"
        assert len(call["messages"]) == 2
        user_content = call["messages"][1]["content"]
        file_blocks = [b for b in user_content if b.get("type") == "file"]
        assert len(file_blocks) == 1
        assert file_blocks[0]["file"]["file_data"].startswith("data:application/pdf;base64,")

    def test_scrape_multiple_files_sequential(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        atts = [
            Attachment(filename=f"fund_{i}.pdf", mime_type="application/pdf", data=b"%PDF")
            for i in range(3)
        ]
        for i in range(3):
            stub_ai_service["responses"].append(("return", _valid_response(f"Fund {i}", "Q3 2024")))

        svc = ScraperService()
        result = svc.scrape_reports(
            attachments=atts,
            keywords=two_keywords,
            llm=_llm(),
        )

        assert len(result.extractions) == 3
        assert [e.filename for e in result.extractions] == [a.filename for a in atts]
        assert len(stub_ai_service["calls"]) == 3


# ---------------------------------------------------------------------------
# TestScraperServicePreconditions
# ---------------------------------------------------------------------------


class TestScraperServicePreconditions:
    def test_raises_unsupported_model_before_any_call(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.capabilities import UnsupportedModelError
        from services.scraper.service import ScraperService

        with pytest.raises(UnsupportedModelError):
            ScraperService().scrape_reports(
                attachments=[pdf_attachment],
                keywords=two_keywords,
                llm=_llm("openai/gpt-4o"),
            )
        assert len(stub_ai_service["calls"]) == 0

    def test_raises_if_prompt_file_missing(
        self, stub_ai_service, monkeypatch, two_keywords, pdf_attachment
    ) -> None:
        from pathlib import Path

        import services.scraper.service as svc_mod
        from services.scraper.service import ScraperService

        monkeypatch.setattr(svc_mod, "_PROMPT_PATH", Path("/nonexistent/Scraper_Prompt.md"))

        with pytest.raises(FileNotFoundError):
            ScraperService().scrape_reports(
                attachments=[pdf_attachment],
                keywords=two_keywords,
                llm=_llm(),
            )


# ---------------------------------------------------------------------------
# TestScraperServiceErrorHandling
# ---------------------------------------------------------------------------


class TestScraperServiceErrorHandling:
    def test_api_error_captured_in_extraction(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.service import ScraperService

        stub_ai_service["responses"].append(("raise", RuntimeError("boom")))

        result = ScraperService().scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert len(result.extractions) == 1
        ext = result.extractions[0]
        assert ext.error is not None
        assert "RuntimeError" in ext.error
        assert "boom" in ext.error
        assert ext.fund_name == ""
        assert ext.period == ""

    def test_parse_error_captured_in_extraction(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.service import ScraperService

        stub_ai_service["responses"].append(("return", "this is not json at all"))

        result = ScraperService().scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        ext = result.extractions[0]
        assert ext.error is not None
        assert "parse" in ext.error.lower()

    def test_run_continues_after_per_file_error(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        att1 = Attachment(filename="bad.pdf", mime_type="application/pdf", data=b"%PDF")
        att2 = Attachment(filename="good.pdf", mime_type="application/pdf", data=b"%PDF")
        stub_ai_service["responses"].append(("raise", RuntimeError("fail 1")))
        stub_ai_service["responses"].append(("return", _valid_response("Fund B", "Q4 2024")))

        result = ScraperService().scrape_reports(
            attachments=[att1, att2],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert len(result.extractions) == 2
        assert result.extractions[0].error is not None
        assert result.extractions[1].error is None
        assert result.extractions[1].fund_name == "Fund B"

    def test_unknown_confidence_defaults_to_not_found(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.models import Confidence
        from services.scraper.service import ScraperService

        payload = {
            "fund_name": "Test Fund",
            "period": "Q1 2025",
            "findings": {
                "NAV": {"value": "99000000", "source": "Page 1", "confidence": "Certain"},
                "IRR": {"value": "12%", "source": "Page 2", "confidence": "High"},
            },
        }
        stub_ai_service["responses"].append(("return", "```json\n" + json.dumps(payload) + "\n```"))

        result = ScraperService().scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        ext = result.extractions[0]
        assert ext.error is None
        nav_finding = ext.findings[0]
        assert nav_finding.confidence == Confidence.NOT_FOUND
        assert nav_finding.value == "99000000"

    def test_missing_finding_in_response_yields_empty_finding(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.models import Confidence
        from services.scraper.service import ScraperService

        payload = {
            "fund_name": "Partial Fund",
            "period": "Q2 2025",
            "findings": {
                "NAV": {"value": "50000000", "source": "Page 5", "confidence": "High"},
                # IRR is absent
            },
        }
        stub_ai_service["responses"].append(("return", "```json\n" + json.dumps(payload) + "\n```"))

        result = ScraperService().scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
        )

        ext = result.extractions[0]
        assert ext.error is None
        assert len(ext.findings) == 2
        irr_finding = ext.findings[1]
        assert irr_finding.keyword.name == "IRR"
        assert irr_finding.value == ""
        assert irr_finding.source == ""
        assert irr_finding.confidence == Confidence.NOT_FOUND


# ---------------------------------------------------------------------------
# TestScraperServiceFileSize
# ---------------------------------------------------------------------------


class TestScraperServiceFileSize:
    def test_rejects_oversize_pdf(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        big_att = Attachment(
            filename="huge.pdf",
            mime_type="application/pdf",
            data=b"x" * (33 * 1024 * 1024),
        )

        result = ScraperService().scrape_reports(
            attachments=[big_att],
            keywords=two_keywords,
            llm=_llm(),
        )

        ext = result.extractions[0]
        assert ext.error is not None
        assert "exceeds" in ext.error
        assert "32 MB" in ext.error
        assert len(stub_ai_service["calls"]) == 0

    def test_accepts_pdf_just_under_limit(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        att = Attachment(
            filename="large.pdf",
            mime_type="application/pdf",
            data=b"x" * (31 * 1024 * 1024),
        )
        stub_ai_service["responses"].append(("return", _valid_response()))

        result = ScraperService().scrape_reports(
            attachments=[att],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert result.extractions[0].error is None
        assert len(stub_ai_service["calls"]) == 1

    def test_text_attachment_not_size_checked(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        att = Attachment(
            filename="notes.md",
            mime_type="text/markdown",
            data="x" * (50 * 1024 * 1024),
        )
        stub_ai_service["responses"].append(("return", _valid_response()))

        result = ScraperService().scrape_reports(
            attachments=[att],
            keywords=two_keywords,
            llm=_llm(),
        )

        assert result.extractions[0].error is None
        assert len(stub_ai_service["calls"]) == 1


# ---------------------------------------------------------------------------
# TestScraperServiceCancellation
# ---------------------------------------------------------------------------


class TestScraperServiceCancellation:
    def test_cancel_before_first_file(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        atts = [
            Attachment(filename=f"f{i}.pdf", mime_type="application/pdf", data=b"%PDF")
            for i in range(3)
        ]

        result = ScraperService().scrape_reports(
            attachments=atts,
            keywords=two_keywords,
            llm=_llm(),
            cancel_check=lambda: True,
        )

        assert result.cancelled is True
        assert len(result.extractions) == 0
        assert len(stub_ai_service["calls"]) == 0

    def test_cancel_between_files(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        atts = [
            Attachment(filename=f"f{i}.pdf", mime_type="application/pdf", data=b"%PDF")
            for i in range(3)
        ]
        stub_ai_service["responses"].append(("return", _valid_response()))
        stub_ai_service["responses"].append(("return", _valid_response()))  # insurance

        state = {"count": 0}

        def cancel() -> bool:
            state["count"] += 1
            return state["count"] > 1  # False on 1st check, True on 2nd

        result = ScraperService().scrape_reports(
            attachments=atts,
            keywords=two_keywords,
            llm=_llm(),
            cancel_check=cancel,
        )

        assert result.cancelled is True
        assert len(result.extractions) == 1

    def test_no_cancel_check_runs_to_completion(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        atts = [
            Attachment(filename=f"f{i}.pdf", mime_type="application/pdf", data=b"%PDF")
            for i in range(2)
        ]
        for _ in range(2):
            stub_ai_service["responses"].append(("return", _valid_response()))

        result = ScraperService().scrape_reports(
            attachments=atts,
            keywords=two_keywords,
            llm=_llm(),
            cancel_check=None,
        )

        assert result.cancelled is False
        assert len(result.extractions) == 2


# ---------------------------------------------------------------------------
# TestScraperServiceProgress
# ---------------------------------------------------------------------------


class TestScraperServiceProgress:
    def test_progress_callback_called_after_each_file(self, stub_ai_service, two_keywords) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        att1 = Attachment(filename="a.pdf", mime_type="application/pdf", data=b"%PDF")
        att2 = Attachment(filename="b.pdf", mime_type="application/pdf", data=b"%PDF")
        stub_ai_service["responses"].append(("return", _valid_response()))
        stub_ai_service["responses"].append(("return", _valid_response()))

        invocations: list[tuple[int, int, str]] = []
        ScraperService().scrape_reports(
            attachments=[att1, att2],
            keywords=two_keywords,
            llm=_llm(),
            progress_callback=lambda d, t, f: invocations.append((d, t, f)),
        )

        assert len(invocations) == 2
        assert invocations[0] == (1, 2, "a.pdf")
        assert invocations[1] == (2, 2, "b.pdf")

    def test_no_progress_callback_works(
        self, stub_ai_service, two_keywords, pdf_attachment
    ) -> None:
        from services.scraper.service import ScraperService

        stub_ai_service["responses"].append(("return", _valid_response()))

        result = ScraperService().scrape_reports(
            attachments=[pdf_attachment],
            keywords=two_keywords,
            llm=_llm(),
            progress_callback=None,
        )

        assert len(result.extractions) == 1

    def test_progress_callback_called_for_errored_files(
        self, stub_ai_service, two_keywords
    ) -> None:
        from services.scraper.models import Attachment
        from services.scraper.service import ScraperService

        att1 = Attachment(filename="err.pdf", mime_type="application/pdf", data=b"%PDF")
        att2 = Attachment(filename="ok.pdf", mime_type="application/pdf", data=b"%PDF")
        stub_ai_service["responses"].append(("raise", RuntimeError("fail")))
        stub_ai_service["responses"].append(("return", _valid_response()))

        invocations: list[tuple[int, int, str]] = []
        ScraperService().scrape_reports(
            attachments=[att1, att2],
            keywords=two_keywords,
            llm=_llm(),
            progress_callback=lambda d, t, f: invocations.append((d, t, f)),
        )

        # Progress is reported even for errored files
        assert (1, 2, "err.pdf") in invocations


# ---------------------------------------------------------------------------
# Profiles, budget and untrusted responses (ADR-0139 §3, §4)
# ---------------------------------------------------------------------------


def _fenced(payload: object) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def _instruction(stub) -> str:
    return stub["calls"][0]["messages"][1]["content"][0]["text"]


def _read(*attachments, keywords=(), **kwargs):
    from services.scraper.service import ScraperService

    return ScraperService().scrape_reports(
        attachments=list(attachments), keywords=list(keywords), llm=_llm(), **kwargs
    )


@pytest.fixture
def next_attachment():
    from services.scraper.models import Attachment

    return Attachment(filename="next.pdf", mime_type="application/pdf", data=b"%PDF next")


class TestScraperServiceContract:
    def test_call_carries_timeout_and_budget(self, stub_ai_service, pdf_attachment) -> None:
        stub_ai_service["responses"].append(("return", _valid_response()))

        _read(pdf_attachment)

        kwargs = stub_ai_service["calls"][0]["kwargs"]
        assert kwargs == {"timeout": 600.0, "max_tokens": 16000}

    def test_prompt_version_is_the_hash_prefix_of_the_prompt(
        self, stub_ai_service, pdf_attachment
    ) -> None:
        import hashlib

        from services.scraper.service import load_scraper_prompt

        stub_ai_service["responses"].append(("return", _valid_response()))

        result = _read(pdf_attachment)

        expected = hashlib.sha256(load_scraper_prompt().encode("utf-8")).hexdigest()[:12]
        assert result.prompt_version == expected

    def test_cancelled_run_carries_the_prompt_version(
        self, stub_ai_service, pdf_attachment
    ) -> None:
        import hashlib

        from services.scraper.service import load_scraper_prompt

        result = _read(pdf_attachment, cancel_check=lambda: True)

        expected = hashlib.sha256(load_scraper_prompt().encode("utf-8")).hexdigest()[:12]
        assert result.cancelled is True
        assert result.prompt_version == expected

    def test_instruction_sentence(self, stub_ai_service, pdf_attachment) -> None:
        stub_ai_service["responses"].append(("return", _valid_response()))

        _read(pdf_attachment)

        assert _instruction(stub_ai_service).split("\n\n")[0] == (
            "Read the attached document as the system prompt describes: classify it, fill the "
            "head, the fields of the chosen profile, the keywords listed below and the reading "
            "note. Return a single JSON object inside a json fenced code block, following the "
            "schema in the system prompt."
        )

    def test_empty_keyword_list(self, stub_ai_service, pdf_attachment) -> None:
        stub_ai_service["responses"].append(("return", _valid_response()))

        result = _read(pdf_attachment)

        assert result.extractions[0].error is None
        assert _instruction(stub_ai_service).endswith("Keywords to extract:\n(none)\n")


class TestScraperServiceProfiles:
    def test_auto_offers_all_profiles(self, stub_ai_service, pdf_attachment, two_keywords) -> None:
        from services.scraper.profiles import PROFILE_IDS

        stub_ai_service["responses"].append(("return", _valid_response()))

        _read(pdf_attachment, keywords=two_keywords)

        block = _instruction(stub_ai_service).split("Keywords to extract:")[0]
        offered = [line.split()[1] for line in block.split("\n") if line.startswith("Profile ")]
        assert offered == list(PROFILE_IDS)
        assert "Document profiles (classify the document as exactly one of them):" in block

    def test_preselected_offers_only_that_one(self, stub_ai_service, pdf_attachment) -> None:
        stub_ai_service["responses"].append(("return", _fenced({"document_type": "pitch_deck"})))

        result = _read(pdf_attachment, profile_id="capital_notice")

        block = _instruction(stub_ai_service).split("Keywords to extract:")[0]
        offered = [line.split()[1] for line in block.split("\n") if line.startswith("Profile ")]
        assert offered == ["capital_notice"]
        assert "Document profile (preselected: read the document as this type):" in block
        assert result.extractions[0].document_type == "capital_notice"
        assert result.extractions[0].profile_version == 1

    def test_auto_takes_the_models_answer(self, stub_ai_service, pdf_attachment) -> None:
        stub_ai_service["responses"].append(("return", _fenced({"document_type": "pitch_deck"})))

        result = _read(pdf_attachment)

        assert result.extractions[0].document_type == "pitch_deck"

    @pytest.mark.parametrize("profile_id", ["dd_report_xyz", ""])
    def test_unknown_profile_id_raises_before_any_call(
        self, stub_ai_service, pdf_attachment, profile_id
    ) -> None:
        from services.scraper.profiles import ProfileError

        with pytest.raises(ProfileError) as exc_info:
            _read(pdf_attachment, profile_id=profile_id)

        assert "dd_report_xyz" not in str(exc_info.value)
        assert stub_ai_service["calls"] == []

    def test_loader_error_is_raised_run_wide(
        self, stub_ai_service, pdf_attachment, monkeypatch
    ) -> None:
        import services.scraper.service as svc_mod
        from services.scraper.profiles import ProfileError

        def _broken():
            raise ProfileError("broken profile file")

        monkeypatch.setattr(svc_mod, "load_profiles", _broken)

        with pytest.raises(ProfileError, match="broken profile file"):
            _read(pdf_attachment)
        assert stub_ai_service["calls"] == []

    def test_profiles_are_loaded_once_per_run(
        self, stub_ai_service, pdf_attachment, next_attachment, monkeypatch
    ) -> None:
        import services.scraper.service as svc_mod

        loads: list[int] = []
        real = svc_mod.load_profiles
        monkeypatch.setattr(svc_mod, "load_profiles", lambda: loads.append(1) or real())
        stub_ai_service["responses"] += [("return", _valid_response())] * 2

        _read(pdf_attachment, next_attachment)

        assert loads == [1]

    def test_bank_keyword_is_not_asked_and_still_reported(
        self, stub_ai_service, pdf_attachment, two_keywords
    ) -> None:
        from services.scraper.mapping import BANK_KEYWORD_SOURCE
        from services.scraper.models import Keyword, KeywordType

        keywords = [two_keywords[0], Keyword("IBAN", KeywordType.TEXT), two_keywords[1]]
        stub_ai_service["responses"].append(("return", _valid_response()))

        result = _read(pdf_attachment, keywords=keywords)

        instruction = _instruction(stub_ai_service)
        assert "IBAN" not in instruction
        assert instruction.endswith("Keywords to extract:\n- NAV (Number)\n- IRR (Percentage)\n")
        findings = result.extractions[0].findings
        assert [f.keyword.name for f in findings] == ["NAV", "IBAN", "IRR"]
        assert findings[1].source == BANK_KEYWORD_SOURCE


class TestScraperServiceUntrustedResponses:
    def test_budget_exceeded_costs_one_file(
        self, stub_ai_service, pdf_attachment, next_attachment
    ) -> None:
        from services.ai_service_core import OutputBudgetExceeded

        stub_ai_service["responses"].append(("raise", OutputBudgetExceeded("stopped")))
        stub_ai_service["responses"].append(("return", _valid_response("Fund B")))

        first, second = _read(pdf_attachment, next_attachment).extractions

        assert first.error == "The reading exceeded the output budget of 16000 tokens."
        assert second.error is None
        assert second.fund_name == "Fund B"

    @pytest.mark.parametrize(
        "response",
        [
            "ZEBRAQUARTZ pay to DE89 3704 0044 0532 0130 00",
            '```json\n{"a": "ZEBRAQUARTZ DE89 3704 0044 0532 0130 00",,}\n```',
        ],
    )
    def test_parse_error_quotes_nothing_of_the_response(
        self, stub_ai_service, pdf_attachment, caplog, response
    ) -> None:
        import logging

        stub_ai_service["responses"].append(("return", response))

        with caplog.at_level(logging.DEBUG):
            extraction = _read(pdf_attachment).extractions[0]

        assert extraction.error == "Could not parse the model's response."
        assert f"{len(response)} characters" in caplog.text
        for secret in ("ZEBRAQUARTZ", "DE89", "0532"):
            assert secret not in extraction.error
            assert secret not in caplog.text

    @pytest.mark.parametrize("payload", [["a", "list"], "a string", 5, None])
    def test_top_level_that_is_not_an_object_costs_one_file(
        self, stub_ai_service, pdf_attachment, next_attachment, payload
    ) -> None:
        stub_ai_service["responses"].append(("return", _fenced(payload)))
        stub_ai_service["responses"].append(("return", _valid_response("Fund B")))

        first, second = _read(pdf_attachment, next_attachment).extractions

        assert first.error == "The model's response is not a JSON object."
        assert second.error is None
        assert second.fund_name == "Fund B"

    @pytest.mark.parametrize("findings", [["NAV"], {"NAV": "125000000"}])
    def test_wrong_shape_below_the_top_is_an_absent_value(
        self, stub_ai_service, pdf_attachment, two_keywords, findings
    ) -> None:
        stub_ai_service["responses"].append(("return", _fenced({"findings": findings})))

        extraction = _read(pdf_attachment, keywords=two_keywords).extractions[0]

        assert extraction.error is None
        assert [f.value for f in extraction.findings] == ["", ""]

    def test_mapping_failure_gives_a_fresh_extraction(
        self, stub_ai_service, pdf_attachment, next_attachment, monkeypatch, caplog
    ) -> None:
        import logging

        import services.scraper.service as svc_mod
        from services.scraper.models import ReportExtraction

        real = svc_mod.apply_parsed_output

        def _half_mapped(extraction, parsed, **kwargs):
            if extraction.filename == "next.pdf":
                return real(extraction, parsed, **kwargs)
            extraction.fund_name = "half mapped"
            raise LookupError("ZEBRAQUARTZ")

        monkeypatch.setattr(svc_mod, "apply_parsed_output", _half_mapped)
        stub_ai_service["responses"] += [("return", _valid_response("Fund B"))] * 2

        with caplog.at_level(logging.DEBUG):
            first, second = _read(pdf_attachment, next_attachment).extractions

        assert first == ReportExtraction(
            filename=pdf_attachment.filename, error="Could not read the model's response."
        )
        assert second.fund_name == "Fund B"
        assert "LookupError" in caplog.text
        assert "ZEBRAQUARTZ" not in caplog.text

    @pytest.mark.parametrize(
        "response",
        [
            '```json\n{"note": ' + "[" * 100_000 + "]" * 100_000 + "}\n```",
            '```json\n{"page_count": ' + "9" * 5000 + "}\n```",
        ],
        ids=["deep-nesting", "huge-integer"],
    )
    def test_json_the_decoder_refuses_costs_one_file(
        self, stub_ai_service, pdf_attachment, next_attachment, response
    ) -> None:
        stub_ai_service["responses"].append(("return", response))
        stub_ai_service["responses"].append(("return", _valid_response("Fund B")))

        first, second = _read(pdf_attachment, next_attachment).extractions

        assert first.error == "Could not read the model's response."
        assert second.fund_name == "Fund B"


class TestScraperServiceFailedCallMessage:
    """P-RD-1b3: the provider's message is filtered before it is shown or logged."""

    def test_bank_details_in_the_exception_reach_neither_error_nor_log(
        self, stub_ai_service, pdf_attachment, next_attachment, caplog
    ) -> None:
        import logging

        from services.scraper.bank_filter import REPLACEMENT

        message = (
            "upstream rejected: IBAN DE89 3704 0044 0532 0130 00, "
            "account number 70872490 in the document"
        )
        stub_ai_service["responses"].append(("raise", ConnectionError(message)))
        stub_ai_service["responses"].append(("return", _valid_response("Fund B")))

        with caplog.at_level(logging.DEBUG):
            first, second = _read(pdf_attachment, next_attachment).extractions

        assert first.error == (
            "API call failed: ConnectionError: upstream rejected: "
            f"IBAN {REPLACEMENT}, account number {REPLACEMENT} in the document"
        )
        for secret in ("DE89", "3704", "0532", "70872490"):
            assert secret not in first.error
            assert secret not in caplog.text
        assert "ConnectionError" in caplog.text
        assert pdf_attachment.filename in caplog.text
        assert "Traceback" not in caplog.text
        assert second.error is None
        assert second.fund_name == "Fund B"
