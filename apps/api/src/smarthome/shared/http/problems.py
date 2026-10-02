"""RFC 9457 problem details, so every error body has the same shape."""

from fastapi.responses import JSONResponse


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    *,
    type_: str = "about:blank",
    extensions: dict[str, object] | None = None,
) -> JSONResponse:
    """`type_` and `extensions` are for problems a client must tell apart and act on."""
    body: dict[str, object] = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    body.update(extensions or {})
    return JSONResponse(body, status_code=status, media_type="application/problem+json")
