"""RFC 9457 problem details, so every error body has the same shape."""

from fastapi.responses import JSONResponse


def problem(status: int, title: str, detail: str | None = None) -> JSONResponse:
    body: dict[str, object] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")
