"""Direct HTTP transport with sanitized failures and receive-only retries."""

import asyncio
import json
import re

import aiohttp
from nagents.channels import ChannelError
from nagents.channels import ChannelValue

from ._validation import json_value

_READ_METHODS = frozenset({"getMe", "getUpdates"})
_MAX_BODY = 8 * 1024 * 1024
_MAX_RETRIES = 3
_MAX_RETRY_DELAY = 60
_FILE_PATH = re.compile(r"[A-Za-z0-9_./-]{1,512}\Z")


class _RequestError(ChannelError):
    def __init__(self, message: str, *, retryable: bool, retry_after: float = 0, outcome_unknown: bool = False) -> None:
        super().__init__(message, retry_after=retry_after, outcome_unknown=outcome_unknown)
        self.retryable = retryable


def retryable(error: BaseException) -> bool:
    """Whether a failed request is transient enough for a listener to poll again later."""
    return isinstance(error, _RequestError) and error.retryable


class Transport:
    def __init__(self, token: str, origin: str) -> None:
        self._token = token
        self._origin = origin
        self._session: aiohttp.ClientSession | None = None

    def open(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(self, method: str, payload: dict[str, ChannelValue], *, timeout: int = 15) -> ChannelValue:
        safe = method in _READ_METHODS
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await self._once(method, payload, timeout=timeout, safe=safe)
            except _RequestError as error:
                if not safe or not error.retryable or attempt == _MAX_RETRIES or error.retry_after > _MAX_RETRY_DELAY:
                    raise
                await asyncio.sleep(max(error.retry_after, float(2**attempt)))
        raise AssertionError("Unreachable retry state")

    async def download(self, file_path: str, *, limit: int) -> bytes:
        """Fetch a ``getFile`` path once; reads are side-effect free, so failures stay sanitized."""
        session = self._session
        if session is None or session.closed:
            raise ChannelError("Telegram channel is not open")
        if not _FILE_PATH.fullmatch(file_path) or ".." in file_path.split("/"):
            raise ChannelError("Telegram returned an invalid file path")
        try:
            async with session.get(
                f"{self._origin}/file/bot{self._token}/{file_path}",
                timeout=aiohttp.ClientTimeout(total=60),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ChannelError(f"Telegram file download failed (HTTP {response.status})")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ChannelError("Telegram attachment exceeds the download limit")
                return bytes(body)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise ChannelError("Telegram file download failed") from None

    async def _once(self, method: str, payload: dict[str, ChannelValue], *, timeout: int, safe: bool) -> ChannelValue:
        session = self._session
        if session is None or session.closed:
            raise ChannelError("Telegram channel is not open")
        try:
            async with session.post(
                f"{self._origin}/bot{self._token}/{method}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                status = response.status
                if status in (401, 403, 409):
                    self._raise_api_error(status, 0, safe=safe)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > _MAX_BODY:
                        raise ValueError("Response too large")
                try:
                    decoded: object = json.loads(body)
                    result = json_value(decoded)
                except (ValueError, RecursionError):
                    if status >= 400:
                        self._raise_api_error(status, 0, safe=safe)
                    raise ValueError("Invalid response") from None
                if not isinstance(result, dict):
                    raise ValueError("Invalid response")
                if status >= 400 or result.get("ok") is False:
                    code = result.get("error_code")
                    if status < 400:
                        if type(code) is not int or not 400 <= code <= 599:
                            raise ValueError("Invalid error response")
                        status = code
                    parameters = result.get("parameters")
                    retry_after: float = 0
                    if isinstance(parameters, dict):
                        delay = parameters.get("retry_after")
                        if type(delay) is int and 0 <= delay <= 2**31 - 1:
                            retry_after = float(delay)
                    self._raise_api_error(status, retry_after, safe=safe)
                if not 200 <= status < 300 or result.get("ok") is not True or "result" not in result:
                    raise ValueError("Invalid response")
                return result["result"]
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, RecursionError):
            raise _RequestError(
                "Telegram request failed or returned an invalid response",
                retryable=True,
                outcome_unknown=not safe,
            ) from None

    @staticmethod
    def _raise_api_error(status: int, retry_after: float, *, safe: bool) -> None:
        messages = {
            401: "Telegram authentication failed (401)",
            403: "Telegram access denied (403)",
            409: "Telegram conflict (409): check for an active webhook or another polling consumer",
            429: "Telegram rate limit exceeded (429)",
        }
        raise _RequestError(
            messages.get(status, f"Telegram API request rejected (HTTP/API {status})"),
            retryable=status == 429 or status >= 500,
            retry_after=retry_after,
            outcome_unknown=not safe and status >= 500,
        )
