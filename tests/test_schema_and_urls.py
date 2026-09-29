"""Schema coercion and relative apply-link resolution."""

import pytest
from pydantic import ValidationError

from ambassador_reader.core import ProgramExtraction, resolve_apply_url

SOURCE = "https://example.edu/programs/ambassador"


def test_null_fields_stay_null():
    record = ProgramExtraction.model_validate(
        {
            "program_name": None,
            "company": None,
            "deadline": None,
            "eligibility": None,
            "benefits": None,
            "apply_url": None,
        }
    )
    assert record.model_dump() == {
        "program_name": None,
        "company": None,
        "deadline": None,
        "eligibility": None,
        "benefits": None,
        "apply_url": None,
    }


def test_blank_strings_become_null():
    record = ProgramExtraction.model_validate(
        {
            "program_name": "  ",
            "company": "",
            "deadline": None,
            "eligibility": " Students only ",
            "benefits": None,
            "apply_url": None,
        }
    )
    assert record.program_name is None
    assert record.company is None
    assert record.eligibility == "Students only"


def test_lists_are_joined_without_adding_facts():
    record = ProgramExtraction.model_validate(
        {
            "program_name": "Fellows",
            "company": "Example",
            "deadline": None,
            "eligibility": ["Open to students", "Must be 18 or older", ""],
            "benefits": ["Mentorship", " $1,000 stipend "],
            "apply_url": None,
        }
    )
    assert record.eligibility == "Open to students; Must be 18 or older"
    assert record.benefits == "Mentorship; $1,000 stipend"


def test_extra_keys_are_ignored():
    record = ProgramExtraction.model_validate(
        {
            "program_name": "Fellows",
            "company": "Example",
            "deadline": 2026,
            "eligibility": None,
            "benefits": None,
            "apply_url": None,
            "confidence": 0.99,
        }
    )
    assert record.deadline == "2026"
    assert "confidence" not in record.model_dump()


def test_missing_key_fails_validation():
    with pytest.raises(ValidationError):
        ProgramExtraction.model_validate({"program_name": "Fellows"})


def test_dict_value_fails_validation():
    with pytest.raises(ValidationError):
        ProgramExtraction.model_validate(
            {
                "program_name": "Fellows",
                "company": {"name": "Example"},
                "deadline": None,
                "eligibility": None,
                "benefits": None,
                "apply_url": None,
            }
        )


def test_root_relative_apply_url():
    assert resolve_apply_url("/apply", SOURCE) == "https://example.edu/apply"


def test_directory_relative_apply_url():
    assert resolve_apply_url("apply", SOURCE) == "https://example.edu/programs/apply"


def test_absolute_apply_url_is_unchanged():
    absolute = "https://jobs.example.edu/ambassadors/apply?ref=1"
    assert resolve_apply_url(absolute, SOURCE) == absolute


def test_null_and_blank_apply_url():
    assert resolve_apply_url(None, SOURCE) is None
    assert resolve_apply_url("  ", SOURCE) is None
    assert resolve_apply_url("see the form at the bottom", SOURCE) is None


def test_non_http_apply_url_is_dropped():
    assert resolve_apply_url("javascript:alert(1)", SOURCE) is None


def test_mailto_apply_url_is_kept():
    assert resolve_apply_url("mailto:ambassadors@example.edu", SOURCE) == (
        "mailto:ambassadors@example.edu"
    )
