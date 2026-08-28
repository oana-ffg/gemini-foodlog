import re
from dataclasses import dataclass

SINGLE_BYTE_RANGE = re.compile(r"^bytes=(?P<start>[0-9]*)-(?P<end>[0-9]*)$")


class RangeNotSatisfiable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


def parse_single_byte_range(value: str, *, total: int) -> ByteRange:
    if total < 1:
        raise ValueError("byte range total must be positive")
    match = SINGLE_BYTE_RANGE.fullmatch(value.strip())
    if match is None:
        raise RangeNotSatisfiable
    start_text = match.group("start")
    end_text = match.group("end")
    if not start_text and not end_text:
        raise RangeNotSatisfiable
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length < 1:
            raise RangeNotSatisfiable
        start = max(0, total - suffix_length)
        end = total - 1
    else:
        start = int(start_text)
        if start >= total:
            raise RangeNotSatisfiable
        end = total - 1 if not end_text else min(int(end_text), total - 1)
        if end < start:
            raise RangeNotSatisfiable
    return ByteRange(start=start, end=end, total=total)
