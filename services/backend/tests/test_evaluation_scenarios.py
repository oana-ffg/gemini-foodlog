import json
from hashlib import sha256
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
SCENARIO_ROOT = FIXTURE_ROOT / "scenarios"
SCHEMA_PATH = REPOSITORY_ROOT / "contracts" / "event-scenario-ground-truth-v1.schema.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def outcomes(event: dict) -> list[dict]:
    return [event["primary_outcome"], *event["acceptable_alternatives"]]


def test_scenario_schema_and_checked_in_drafts_are_valid() -> None:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    scenario_paths = sorted(SCENARIO_ROOT.glob("*.json"))

    assert scenario_paths
    scenario_ids: set[str] = set()
    for scenario_path in scenario_paths:
        scenario = load_json(scenario_path)
        validator.validate(scenario)
        assert scenario["scenario_id"] not in scenario_ids
        scenario_ids.add(scenario["scenario_id"])
        if scenario["review_status"] == "draft":
            assert scenario["human_review"] is None
        else:
            assert scenario["human_review"] is not None


def test_scenario_references_and_immutable_fixture_hashes_are_consistent() -> None:
    for scenario_path in sorted(SCENARIO_ROOT.glob("*.json")):
        scenario = load_json(scenario_path)
        observations = {item["observation_id"]: item for item in scenario["observations"]}
        contexts = {item["context_id"]: item for item in scenario["context"]}
        events = {item["event_id"]: item for item in scenario["expected_events"]}

        assert len(observations) == len(scenario["observations"])
        assert len(contexts) == len(scenario["context"])
        assert len(events) == len(scenario["expected_events"])

        assigned_observations: set[str] = set()
        for observation in observations.values():
            fixture_path = (FIXTURE_ROOT / observation["file_path"]).resolve()
            assert fixture_path.is_relative_to(FIXTURE_ROOT.resolve())
            assert fixture_path.is_file()
            assert sha256(fixture_path.read_bytes()).hexdigest() == observation["sha256"]

        for event in events.values():
            event_observations = set(event["observation_ids"])
            assert event_observations <= observations.keys()
            assert not assigned_observations & event_observations
            assigned_observations |= event_observations
            assert event["start_offset_seconds"] <= event["end_offset_seconds"]
            for observation_id in event_observations:
                offset = observations[observation_id]["offset_seconds"]
                assert event["start_offset_seconds"] <= offset <= event["end_offset_seconds"]

            evidence_ids: set[str] = set()
            for evidence in event["required_evidence"]:
                assert evidence["evidence_id"] not in evidence_ids
                evidence_ids.add(evidence["evidence_id"])
                known_sources = observations if evidence["source_kind"] == "visual" else contexts
                assert set(evidence["source_ids"]) <= known_sources.keys()

        assert assigned_observations == observations.keys()


def test_scenario_outcomes_and_question_policies_are_state_correct() -> None:
    for scenario_path in sorted(SCENARIO_ROOT.glob("*.json")):
        scenario = load_json(scenario_path)
        for event in scenario["expected_events"]:
            concept_ids: set[str] = set()
            for outcome in outcomes(event):
                if outcome["activity_kind"] == "tentative_meal":
                    assert outcome["accepted_labels"]
                    assert outcome["components"]
                else:
                    assert outcome["components"] == []
                for component in outcome["components"]:
                    concept_ids.add(component["component_id"])
                    concept_ids.update(
                        item["concept_id"] for item in component["required_ingredients"]
                    )

            question = event["question_expectation"]
            if question["policy"] == "required":
                assert set(question["candidate_concepts"]) <= concept_ids
            if question["policy"] == "forbidden":
                assert question["candidate_concepts"] == []
                assert question["acceptable_impacts"] == []
