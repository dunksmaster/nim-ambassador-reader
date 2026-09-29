"""JSON extraction from fenced, prose, and empty model replies."""

import pytest

from ambassador_reader.core import ExtractionError, parse_program_json

VALID = """{
  "program_name": "Campus Ambassadors",
  "company": "Example Co",
  "deadline": null,
  "eligibility": "Students in Canada",
  "benefits": "A $500 stipend",
  "apply_url": "/apply"
}"""


def test_parses_raw_json():
    record = parse_program_json(VALID)
    assert record.program_name == "Campus Ambassadors"
    assert record.deadline is None
    assert record.apply_url == "/apply"


def test_parses_json_fence_with_prose():
    text = (
        "Here is the extraction:\n"
        "```json\n"
        f"{VALID}\n"
        "```\n"
        "Let me know if you need anything else."
    )
    record = parse_program_json(text)
    assert record.company == "Example Co"
    assert record.benefits == "A $500 stipend"


def test_parses_fence_without_language_tag():
    text = f"```\n{VALID}\n```"
    record = parse_program_json(text)
    assert record.eligibility == "Students in Canada"


def test_parses_prose_around_unfenced_json():
    text = f"Sure — the fields are below.\n{VALID}\nThose are only the stated facts."
    record = parse_program_json(text)
    assert record.program_name == "Campus Ambassadors"


def test_ignores_think_block_and_a_smaller_object():
    text = (
        "<think>The page might be {\"note\": \"ignore me\"} but I should not guess.</think>\n"
        f"```json\n{VALID}\n```"
    )
    record = parse_program_json(text)
    assert record.program_name == "Campus Ambassadors"


def test_empty_response_raises():
    with pytest.raises(ExtractionError):
        parse_program_json("   ")


def test_prose_without_json_raises():
    with pytest.raises(ExtractionError):
        parse_program_json("I could not find a program on this page.")


def test_object_missing_keys_raises():
    with pytest.raises(ExtractionError):
        parse_program_json('{"program_name": "Only one field"}')
