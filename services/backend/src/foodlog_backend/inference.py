from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Protocol

from .models import Confidence, MealComponent, MealInference


class InferenceEngine(Protocol):
    async def infer(self, image: bytes, content_type: str) -> MealInference: ...


class FixtureInferenceEngine:
    """Zero-cost local engine for known immutable synthetic fixtures only."""

    _KNOWN: ClassVar[dict[str, MealInference]] = {
        "8e51f9691aebf6335d6d1cf1c7863b73654918bb97db3bd99bd5235e290da208": MealInference(
            title="Air-fried steak",
            confidence=Confidence.CONFIDENT,
            components=[
                MealComponent(
                    name="Steak",
                    ingredients=["beef steak"],
                    preparation_methods=["air frying"],
                )
            ],
            observations=[
                "A raw red steak is inside a black air-fryer basket.",
                "The basket is on the counter beside the sink.",
            ],
            alternatives=["another cut of red meat"],
            rationale="The meat is visibly red and placed in an air-fryer basket for cooking.",
        ),
        "7cf7f1c875e74bc5badeb79d1fbfc6656bed6fb782f6b1766c2772dca434cd97": MealInference(
            title="Air-fried chicken breast",
            confidence=Confidence.CONFIDENT,
            components=[
                MealComponent(
                    name="Chicken breast",
                    ingredients=["chicken breast"],
                    preparation_methods=["air frying"],
                )
            ],
            observations=[
                "Two raw pale poultry fillets are inside a black air-fryer basket.",
                "The basket is on the counter beside the sink.",
            ],
            alternatives=["turkey breast"],
            rationale="The pale raw fillets have the shape and texture of chicken breasts.",
        ),
        "3fbc475d0d68302aed46aa5af483ba50367165918e8aa988eeb857bf55ecd848": MealInference(
            title="Reheated tomato pasta",
            confidence=Confidence.LIKELY,
            components=[
                MealComponent(
                    name="Tomato fusilli",
                    ingredients=["fusilli pasta", "tomato sauce"],
                    preparation_methods=["reheating"],
                )
            ],
            observations=[
                "Cooked tomato-coated fusilli is being transferred from a storage container.",
                "A microwave is visible behind the leftovers.",
            ],
            alternatives=["pasta being served cold"],
            rationale=(
                "The storage container and nearby microwave make reheating the "
                "likely activity."
            ),
        ),
    }

    async def infer(self, image: bytes, content_type: str) -> MealInference:
        del content_type
        digest = sha256(image).hexdigest()
        known = self._KNOWN.get(digest)
        if known:
            return known.model_copy(deep=True)
        return MealInference(
            title="Unrecognized kitchen activity",
            confidence=Confidence.UNCERTAIN,
            components=[],
            observations=[
                "A kitchen frame was accepted, but local fixture inference has no "
                "ground truth."
            ],
            alternatives=[],
            rationale=(
                "Local mode never calls Gemini. Only immutable synthetic fixtures "
                "have deterministic results; production inference remains disabled "
                "until explicitly configured."
            ),
            clarification_question="What meal or ingredient was being prepared?",
            clarification_reason=(
                "The local fixture engine cannot identify this frame, and the answer "
                "would turn an unresolved journal entry into a confirmed household event."
            ),
        )


def verify_fixture_files(fixtures_directory: Path) -> list[str]:
    mismatches: list[str] = []
    for file_path in fixtures_directory.glob("*.png"):
        digest = sha256(file_path.read_bytes()).hexdigest()
        if digest not in FixtureInferenceEngine._KNOWN:
            mismatches.append(file_path.name)
    return sorted(mismatches)
