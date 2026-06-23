"""Firecrawl-compatible /v1/scrape endpoint (minimal subset)."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from hermes_local_web_extract.config import Settings, get_settings
from hermes_local_web_extract.errors import ExtractException
from hermes_local_web_extract.models import (
    ErrorResponse,
    ExtractError,
    ExtractErrorCode,
    ExtractRequest,
    FirecrawlData,
    FirecrawlMetadata,
    FirecrawlScrapeRequest,
    FirecrawlScrapeResponse,
    OutputFormat,
    RenderJs,
)
from hermes_local_web_extract.pipeline import run_extraction
from hermes_local_web_extract.rate_limit import get_limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["firecrawl-compat"])

_SUPPORTED_FORMATS = {"markdown", "html", "text"}


@router.post(
    "/v1/scrape",
    response_model=FirecrawlScrapeResponse,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Firecrawl-compatible scrape endpoint (minimal subset)",
    description=(
        "Implements a practical subset of the Firecrawl /v1/scrape API. "
        "Not all Firecrawl fields are supported. See docs/limitations.md."
    ),
)
@router.post(
    "/v2/scrape",
    response_model=FirecrawlScrapeResponse,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Firecrawl-compatible scrape endpoint (v2 alias of /v1/scrape)",
    description=(
        "Same as /v1/scrape — aliased so the Firecrawl v2 SDK can target "
        "this server without changing FIRECRAWL_API_URL."
    ),
)
async def scrape(
    request: Request,
    body: FirecrawlScrapeRequest,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    limiter = get_limiter()
    try:
        limiter.check(request)
    except ExtractException as exc:
        return JSONResponse(
            status_code=429,
            content=ErrorResponse(
                error=ExtractError(code=exc.code, message=exc.message)
            ).model_dump(),
        )

    # Map Firecrawl formats to native formats
    requested_formats: list[OutputFormat] = []
    unsupported: list[str] = []
    for fmt in body.formats:
        if fmt in _SUPPORTED_FORMATS:
            requested_formats.append(OutputFormat(fmt))
        else:
            unsupported.append(fmt)

    if not requested_formats:
        requested_formats = [OutputFormat.markdown]

    # Always include metadata for the compat response
    if OutputFormat.metadata not in requested_formats:
        requested_formats.append(OutputFormat.metadata)

    extract_request = ExtractRequest(
        url=body.url,
        formats=requested_formats,
        render_js=RenderJs.never,
        timeout_seconds=body.timeout_seconds(),
        include_metadata=True,
        include_links=False,
        include_images=False,
    )

    try:
        result = await run_extraction(extract_request, settings)
    except ExtractException as exc:
        status = 400
        if exc.code == ExtractErrorCode.RATE_LIMITED:
            status = 429
        elif exc.code == ExtractErrorCode.INTERNAL_ERROR:
            status = 500
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=ExtractError(code=exc.code, message=exc.message)
            ).model_dump(),
        )
    except Exception:
        logger.exception("Unexpected error during firecrawl-compat extraction")
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error=ExtractError(
                    code=ExtractErrorCode.INTERNAL_ERROR,
                    message="An unexpected internal error occurred.",
                )
            ).model_dump(),
        )

    meta = FirecrawlMetadata(
        title=result.title,
        description=result.description,
        sourceURL=result.url,
        url=result.final_url,
        statusCode=result.metadata.status_code if result.metadata else None,
        contentType=result.metadata.content_type if result.metadata else None,
        extractor=result.metadata.extractor if result.metadata else None,
    )

    data = FirecrawlData(
        markdown=result.markdown,
        html=result.html,
        metadata=meta,
    )

    response = FirecrawlScrapeResponse(success=True, data=data)

    all_warnings = list(result.warnings or [])
    if unsupported:
        all_warnings.append(f"Unsupported formats ignored: {unsupported}")
    if all_warnings:
        logger.debug("Firecrawl-compat warnings: %s", all_warnings)

    return JSONResponse(content=response.model_dump())
