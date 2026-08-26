from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RawMailStoredEventV1(BaseModel):
    """Metadata-only handoff emitted by the untrusted inbound-mail gateway."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["raw_mail_stored"] = "raw_mail_stored"
    account_id: str = Field(min_length=1, max_length=128)
    mail_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_class: Literal["untrusted_external"] = "untrusted_external"
