"""Validate configured endpoints before attaching proxy credentials."""

from urllib.parse import urlsplit


def validate_endpoint(url: str, *, allow_http_loopback: bool = False) -> str:
    """Require HTTPS; optionally allow unauthenticated local ComfyUI test servers."""
    error = "Backend URL must be HTTPS with a valid host and no credentials, query or fragment"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError(error) from None
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or port == 0
        or any(char.isspace() or ord(char) < 32 or char == "\\" for char in url)
    ):
        raise ValueError(error)
    local_http = (
        allow_http_loopback
        and parts.scheme == "http"
        and parts.hostname in ("localhost", "127.0.0.1", "::1")
    )
    if parts.scheme != "https" and not local_http:
        raise ValueError(error)
    return url.rstrip("/")


def redirect_guard():
    """Stop redirects before aiohttp can forward custom Modal auth headers."""
    import aiohttp

    async def reject_redirect(session, context, params):
        raise ValueError("Backend redirects are not allowed; configure the final HTTPS endpoint")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    return trace
