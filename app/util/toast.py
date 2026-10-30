import json
from typing import Literal, final

from fastapi import Response


def toast_response(
    message: str, type: Literal["error", "success", "info"] = "success"
) -> Response:
    """An empty htmx response that only shows a toast.

    Safe for text from outside, such as a book or author name: it travels as
    JSON in a header, never as markup or script.
    """
    return Response(
        headers={
            "HX-Trigger": json.dumps({"toast": {"type": type, "message": message}})
        }
    )


@final
class ToastException(Exception):
    """Shows a toast on the frontend if raised on an HTMX endpoint"""

    def __init__(
        self,
        message: str,
        type: Literal["error", "success", "info"] = "error",
        cause_refresh: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.type = type
        self.force_refresh = cause_refresh
