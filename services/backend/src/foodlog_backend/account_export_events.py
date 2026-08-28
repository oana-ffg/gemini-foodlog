from typing import Literal, Protocol

from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict, Field

from .pubsub import PubSubJsonPublisher


class AccountExportRequestedEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["account_export_requested"] = "account_export_requested"
    account_id: str = Field(min_length=1, max_length=128)
    export_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class AccountExportEventPublisher(Protocol):
    async def publish(self, event: AccountExportRequestedEventV1) -> str: ...


class InMemoryAccountExportEventPublisher:
    def __init__(self) -> None:
        self.events: list[AccountExportRequestedEventV1] = []
        self.failure: Exception | None = None

    async def publish(self, event: AccountExportRequestedEventV1) -> str:
        if self.failure is not None:
            raise self.failure
        self.events.append(event.model_copy(deep=True))
        return f"memory-export-message-{len(self.events)}"


class PubSubAccountExportEventPublisher:
    def __init__(
        self,
        *,
        topic: str,
        client: pubsub_v1.PublisherClient | None = None,
    ) -> None:
        self._publisher = PubSubJsonPublisher(topic=topic, client=client)

    async def publish(self, event: AccountExportRequestedEventV1) -> str:
        return await self._publisher.publish(
            event,
            event_kind=event.kind,
            schema_version=event.schema_version,
        )
