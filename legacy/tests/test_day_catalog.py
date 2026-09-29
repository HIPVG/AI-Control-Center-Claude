from pathlib import Path

import yaml

from backend.control.local_llm_day_program import LocalLLMDayProgram


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "day-contract"


def test_catalog_exposes_all_day_contract_admission_inputs_without_execution():
    program = LocalLLMDayProgram(FIXTURE_ROOT)

    catalog = program.days()

    assert [entry["day"] for entry in catalog] == list(range(1, 15))
    assert all(entry["admission"] == {
        "status": "ADMISSIBLE",
        "next_state": "PREFLIGHT",
        "reason_code": None,
        "next_action": "Supply a matching RunIntent and evaluate deterministic preflight before Go.",
    } for entry in catalog)
    assert all(entry["contract_version"] == "2026-09-22-v2-evidence-contract"
               for entry in catalog)
    assert all(entry["contract_fingerprint"] for entry in catalog)
    assert all(entry["required_evidence"] for entry in catalog)
    assert all(entry["source_scope"] and all(source["present"] for source in entry["source_scope"])
               for entry in catalog)
    assert all(entry["preflight_inputs"] == [
        "selected_day", "contract_fingerprint", "git_fingerprint",
        "effective_permission", "requested_limits", "external_prerequisite",
    ] for entry in catalog)
    assert all(entry["execution_started"] is False for entry in catalog)
    assert program.snapshot.selected_day is None
    assert program.snapshot.state.value == "IDLE"


def test_invalid_day_definition_blocks_only_that_day(tmp_path):
    document = yaml.safe_load(LocalLLMDayProgram.PROGRAM_PATH.read_text(encoding="utf-8"))
    del document["days"][5]["objective"]
    config = tmp_path / "local_llm_day_program.yaml"
    config.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    program = LocalLLMDayProgram(FIXTURE_ROOT)
    program.PROGRAM_PATH = config

    catalog = program.days()

    day6 = catalog[5]
    assert (day6["admission"]["status"], day6["admission"]["next_state"],
            day6["admission"]["reason_code"]) == (
        "BLOCKED", "INPUT_BLOCKED", "DAY_DEFINITION_INVALID")
    assert day6["execution_started"] is False
    assert all(entry["admission"]["status"] == "ADMISSIBLE"
               for entry in catalog if entry["day"] != 6)
    assert program.snapshot.selected_day is None
    assert program.snapshot.state.value == "IDLE"
