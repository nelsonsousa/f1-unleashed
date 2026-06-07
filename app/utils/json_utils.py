"""Custom JSON utilities for handling F1 data with NaN values."""

import json
import math
from typing import Any
from fastapi.responses import JSONResponse


class NaNSafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that converts NaN and Inf to None."""

    def default(self, obj: Any) -> Any:
        return super().default(obj)

    def encode(self, obj: Any) -> str:
        return super().encode(self._clean_nan(obj))

    def _clean_nan(self, obj: Any) -> Any:
        """Recursively replace NaN/Inf with None."""
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        elif isinstance(obj, dict):
            return {k: self._clean_nan(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._clean_nan(item) for item in obj]
        return obj


def clean_nan(obj: Any) -> Any:
    """Clean NaN/Inf values from an object for JSON serialization."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [clean_nan(item) for item in obj]
    return obj


class NaNSafeJSONResponse(JSONResponse):
    """JSONResponse that handles NaN values."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            clean_nan(content),
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")
