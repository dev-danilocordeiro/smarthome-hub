"""Optimistic concurrency over HTTP: versioned resources carry an ETag, edits send If-Match."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from smarthome.shared.http.problems import problem


class PreconditionRequired(Exception):
    pass


def etag(version: int) -> str:
    return f'"{version}"'


def expected_version(if_match: str | None, *, required: bool) -> int | None:
    """Edits name the version they started from."""
    if if_match is None:
        if required:
            raise PreconditionRequired
        return None
    raw = if_match.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError:
        return -1  # never current: 412


async def _precondition_required(_: Request, __: Exception) -> JSONResponse:
    return problem(
        status.HTTP_428_PRECONDITION_REQUIRED,
        "If-Match required",
        "Send the ETag of the version you edited, so a concurrent edit is not overwritten.",
    )


def register(app: FastAPI) -> None:
    app.add_exception_handler(PreconditionRequired, _precondition_required)
