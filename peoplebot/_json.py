"""Stable JSON encoding for experimental PeopleBot records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def stable_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode a mapping as stable UTF-8 JSON with one trailing newline."""

    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
