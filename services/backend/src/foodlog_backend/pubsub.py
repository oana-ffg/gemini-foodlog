import asyncio
import json
from base64 import b64decode
from binascii import Error as Base64Error

from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PubSubMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: str
    message_id: str = Field(alias="messageId")


class PubSubPushEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: PubSubMessage
    subscription: str
    delivery_attempt: int = Field(default=1, alias="deliveryAttempt", ge=1)


def decode_event[EventT: BaseModel](
    envelope: PubSubPushEnvelope,
    event_type: type[EventT],
    *,
    event_name: str,
) -> EventT:
    try:
        content = b64decode(envelope.message.data, validate=True)
        payload = json.loads(content)
        return event_type.model_validate(payload)
    except (Base64Error, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"invalid {event_name} Pub/Sub event") from error


class PubSubJsonPublisher:
    def __init__(
        self,
        *,
        topic: str,
        client: pubsub_v1.PublisherClient | None = None,
    ) -> None:
        self._topic = topic
        self._client = client or pubsub_v1.PublisherClient()

    async def publish(self, event: BaseModel, *, event_kind: str, schema_version: int) -> str:
        content = json.dumps(
            event.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        future = self._client.publish(
            self._topic,
            content,
            event_kind=event_kind,
            schema_version=str(schema_version),
        )
        return await asyncio.to_thread(future.result, timeout=30)
