from __future__ import annotations


def base_payload() -> dict[str, object]:
    return {
        "schema_version": "activity-meal-inference-v1",
        "event_id": "event-001",
        "source_capture_ids": ["capture-001"],
        "kind": "tentative_meal",
        "best_guess": "Air-fried steak",
        "confidence": "uncertain",
        "components": [
            {
                "id": "steak",
                "name": "Steak",
                "ingredients": ["red meat"],
                "preparation_methods": ["air frying"],
                "confidence": "uncertain",
                "alternatives": [
                    {
                        "label": "Air-fried lamb",
                        "reason": "The distant view does not show the cut clearly.",
                        "evidence_ids": ["obs_meat"],
                    }
                ],
                "evidence_ids": ["obs_meat", "ctx_order", "ded_meat"],
            }
        ],
        "direct_observations": [
            {
                "id": "obs_meat",
                "description": "Red meat is visible beside an air-fryer basket.",
                "image_evidence": [
                    {
                        "capture_id": "capture-001",
                        "region": {"x": 0.55, "y": 0.5, "width": 0.3, "height": 0.3},
                    }
                ],
            }
        ],
        "contextual_evidence": [
            {
                "id": "ctx_order",
                "description": "A recent order contains beef steak.",
                "source_kind": "purchase",
                "source_id": "purchase-001",
            }
        ],
        "assumptions": [],
        "deductions": [
            {
                "id": "ded_meat",
                "description": "The activity most likely prepares a red-meat meal.",
                "evidence_ids": ["obs_meat", "ctx_order"],
            }
        ],
        "alternatives": [
            {
                "label": "Air-fried lamb",
                "reason": "The meat color fits, but the cut is unclear.",
                "evidence_ids": ["obs_meat"],
            }
        ],
        "rationale": (
            "Visible red meat and the recent steak purchase support steak, but the cut is unclear."
        ),
        "question": {
            "prompt": "Was this the recently purchased steak or lamb from elsewhere?",
            "justification": "The answer distinguishes the two supported red-meat hypotheses.",
            "evidence_ids": ["obs_meat", "ctx_order"],
            "candidate_labels": ["Air-fried steak", "Air-fried lamb"],
            "impact": "changes_meal_identity",
        },
        "allowed_actions": ["confirm_guess", "correct", "discard_not_cooking"],
    }
