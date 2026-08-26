import argparse
import json
from pathlib import Path

from foodlog_agent.inference_schema import ActivityMealInferenceV1
from foodlog_backend.app import create_app
from foodlog_backend.auth import VerifiedIdentity
from foodlog_backend.models import CaptureEnvelopeV1
from foodlog_backend.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIRECTORY = REPOSITORY_ROOT / "contracts"


class ContractTokenVerifier:
    async def verify(self, _token: str) -> VerifiedIdentity:
        raise RuntimeError("Contract generation never verifies a runtime token")


def canonical_json(value: object) -> str:
    return f"{json.dumps(value, indent=2, sort_keys=True)}\n"


def generated_contracts() -> dict[Path, str]:
    app = create_app(
        Settings(
            environment="test",
            auth_backend="firebase",
            firebase_project_id="contract-generation",
        ),
        token_verifier=ContractTokenVerifier(),
    )
    return {
        CONTRACTS_DIRECTORY / "activity-meal-inference-v1.schema.json": canonical_json(
            ActivityMealInferenceV1.model_json_schema()
        ),
        CONTRACTS_DIRECTORY / "capture-envelope-v1.schema.json": canonical_json(
            CaptureEnvelopeV1.model_json_schema()
        ),
        CONTRACTS_DIRECTORY / "openapi.json": canonical_json(app.openapi()),
    }


def write_contracts() -> None:
    CONTRACTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for path, content in generated_contracts().items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPOSITORY_ROOT)}")


def check_contracts() -> int:
    stale = [
        path
        for path, expected in generated_contracts().items()
        if not path.exists() or path.read_text(encoding="utf-8") != expected
    ]
    if stale:
        for path in stale:
            print(f"out of date: {path.relative_to(REPOSITORY_ROOT)}")
        print("run: cd services/backend && uv run python scripts/generate_contracts.py")
        return 1
    print("checked-in API contracts are current")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic FoodLog API contracts")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when checked-in contracts differ",
    )
    args = parser.parse_args()
    if args.check:
        return check_contracts()
    write_contracts()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
