from typing import Literal, Protocol

from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict

from .pubsub import PubSubJsonPublisher


class CaptureStoredEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["capture_stored"] = "capture_stored"
    account_id: str
    capture_id: str


class CaptureEventPublisher(Protocol):
    async def publish(self, event: CaptureStoredEventV1) -> str: ...


class InMemoryCaptureEventPublisher:
    def __init__(self) -> None:
        self.events: list[CaptureStoredEventV1] = []
        self.failure: Exception | None = None

    async def publish(self, event: CaptureStoredEventV1) -> str:
        if self.failure is not None:
            raise self.failure
        self.events.append(event.model_copy(deep=True))
        return f"memory-image-message-{len(self.events)}"


class PubSubCaptureEventPublisher:
    def __init__(
        self,
        *,
        topic: str,
        client: pubsub_v1.PublisherClient | None = None,
    ) -> None:
        self._publisher = PubSubJsonPublisher(topic=topic, client=client)

    async def publish(self, event: CaptureStoredEventV1) -> str:
        return await self._publisher.publish(
            event,
            event_kind=event.kind,
            schema_version=event.schema_version,
        )
