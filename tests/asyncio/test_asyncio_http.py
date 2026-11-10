import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import logging
import socket
import threading
import warnings

from aiohttp import ClientConnectorError, ClientResponseError, ClientSession, ContentTypeError, TCPConnector
from aiohttp_retry import ExponentialRetry
import pytest
from yarl import URL

from pydantic import SecretStr, ValidationError

from koheesio.asyncio.http import AsyncHttpStep
from koheesio.steps.http import HttpMethod


@pytest.fixture
def get_endpoint(fake_http_server: str) -> URL:
    return URL(f"{fake_http_server}/get")


@pytest.fixture
def status_503_endpoint(fake_http_server: str) -> URL:
    return URL(f"{fake_http_server}/status/503")


@pytest.fixture
def status_404_endpoint(fake_http_server: str) -> URL:
    return URL(f"{fake_http_server}/status/404")


@pytest.mark.asyncio
def test_async_http_get_step_positive(get_endpoint: URL) -> None:
    """Testing the GET function with a positive scenario."""
    step = AsyncHttpStep(
        method=HttpMethod.GET,
        url=[get_endpoint] * 10,
    )
    step.execute()
    responses_urls = step.output.responses_urls

    assert len(responses_urls) == 10
    response, url = responses_urls[0]
    assert url == get_endpoint
    assert response["url"] == str(get_endpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http_method, endpoint_fixture, expected_status",
    [
        (HttpMethod.GET, "status_503_endpoint", 503),
        (HttpMethod.GET, "status_404_endpoint", 404),
        (HttpMethod.POST, "status_503_endpoint", 503),
        (HttpMethod.POST, "status_404_endpoint", 404),
        (HttpMethod.PUT, "status_503_endpoint", 503),
        (HttpMethod.PUT, "status_404_endpoint", 404),
        (HttpMethod.DELETE, "status_503_endpoint", 503),
        (HttpMethod.DELETE, "status_404_endpoint", 404),
    ],
)
def test_async_http_step_negative(
    request: pytest.FixtureRequest,
    http_method: HttpMethod,
    endpoint_fixture: str,
    expected_status: int,
) -> None:
    """Testing the function with a negative scenario (503 and 404 status codes)."""
    endpoint: URL = request.getfixturevalue(endpoint_fixture)
    step = AsyncHttpStep(method=http_method, url=[endpoint])
    with pytest.raises(ClientResponseError) as excinfo:
        step.execute()

    assert excinfo.value.status == expected_status


@pytest.mark.asyncio
async def test_async_http_step(get_endpoint: URL) -> None:
    """Testing the AsyncHttpStep class."""
    step = AsyncHttpStep(
        client_session=ClientSession(),
        connector=TCPConnector(limit=10),
        urls=[get_endpoint, get_endpoint],
        retry_options=ExponentialRetry(),
        headers={"Content-Type": "application/json"},
    )

    responses_urls = await step.get()

    assert isinstance(responses_urls, list)
    assert len(responses_urls) == 2


@pytest.mark.asyncio
async def test_async_http_step_with_timeout(get_endpoint: URL) -> None:
    """Testing the AsyncHttpStep class with timeout."""
    with pytest.raises(ValidationError):
        AsyncHttpStep(
            client_session=ClientSession(),
            connector=TCPConnector(limit=10),
            timeout=10,
            urls=[get_endpoint, get_endpoint],
            retry_options=ExponentialRetry(),
            headers={"Content-Type": "application/json"},
        )


def test_async_http_step_with_invalid_http_method(get_endpoint: URL) -> None:
    """Testing the function with an invalid HTTP method."""
    invalid_method = "INVALID_METHOD"

    with pytest.raises(ValueError) as exc_info:
        step = AsyncHttpStep(method=invalid_method, url=[get_endpoint])
        step.execute()

    assert str(exc_info.value) == f"Method {invalid_method} not implemented in AsyncHttpStep."


@pytest.mark.asyncio
async def test_async_http_step_set_outputs_warning(get_endpoint: URL) -> None:
    """Testing the AsyncHttpStep class's set_outputs method for warning."""
    step = AsyncHttpStep(
        client_session=ClientSession(),
        connector=TCPConnector(limit=10),
        urls=[get_endpoint, get_endpoint],
        retry_options=ExponentialRetry(),
        headers={"Content-Type": "application/json"},
    )

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        step.set_outputs(None)
        assert len(w) == 1
        assert issubclass(w[-1].category, UserWarning)
        assert "set outputs is not implemented in AsyncHttpStep." == str(w[-1].message)


@pytest.mark.asyncio
async def test_async_http_step_get_options_warning(get_endpoint: URL) -> None:
    """Testing the AsyncHttpStep class's get_options method for warning."""
    step = AsyncHttpStep(
        client_session=ClientSession(),
        connector=TCPConnector(limit=10),
        urls=[get_endpoint, get_endpoint],
        retry_options=ExponentialRetry(),
        headers={"Content-Type": "application/json"},
    )

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        step.get_options()
        assert len(w) == 1
        assert issubclass(w[-1].category, UserWarning)
        assert "get_options is not implemented in AsyncHttpStep." == str(w[-1].message)


@pytest.mark.asyncio
async def test_owned_session_repeated_get(get_endpoint: URL) -> None:
    step = AsyncHttpStep(url=[get_endpoint])
    first = await step.get()
    second = await step.get()
    assert first == second == [({"url": str(get_endpoint)}, get_endpoint)]
    assert step.client_session is None
    assert step.connector is None


def test_owned_session_repeated_execute(get_endpoint: URL) -> None:
    step = AsyncHttpStep(url=[get_endpoint])
    first = step.execute().responses_urls
    second = step.execute().responses_urls
    assert first == second == [({"url": str(get_endpoint)}, get_endpoint)]
    assert step.client_session is None
    assert step.connector is None


@pytest.mark.asyncio
async def test_owned_session_error_then_retry(status_404_endpoint: URL, get_endpoint: URL) -> None:
    step = AsyncHttpStep(url=[status_404_endpoint], retry_options=ExponentialRetry(attempts=1))
    with pytest.raises(ClientResponseError):
        await step.get()
    step.url = [get_endpoint]
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]
    assert step.client_session is None
    assert step.connector is None


@pytest.mark.asyncio
async def test_owned_session_empty_then_get(get_endpoint: URL) -> None:
    step = AsyncHttpStep(url=[])
    assert await step.get() == []
    step.url = [get_endpoint]
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]
    assert step.client_session is None
    assert step.connector is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "request_error"])
async def test_owned_session_drains_tasks_and_can_retry(monkeypatch, get_endpoint: URL, failure: str) -> None:
    step = AsyncHttpStep(url=[get_endpoint, get_endpoint.with_query(index=1)])
    started = asyncio.Event()
    finished = asyncio.Event()
    resources = []
    original_request = AsyncHttpStep.request

    async def controlled_request(self, method, url, **kwargs):
        if url.query:
            await started.wait()
            if failure == "request_error":
                raise ValueError("synthetic request error")
            await asyncio.Event().wait()
        resources.extend([self.client_session, self.connector])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr(AsyncHttpStep, "request", controlled_request)
    operation = asyncio.create_task(step.get())
    await asyncio.wait_for(started.wait(), timeout=5)
    if failure == "cancel":
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        with pytest.raises(ValueError, match="synthetic request error"):
            await operation
    assert finished.is_set()
    assert all(resource.closed for resource in resources)
    assert step.client_session is None
    assert step.connector is None
    monkeypatch.setattr(AsyncHttpStep, "request", original_request)
    step.url = [get_endpoint]
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]


@pytest.mark.asyncio
async def test_supplied_session_keeps_legacy_close_behavior(get_endpoint: URL) -> None:
    session = ClientSession()
    step = AsyncHttpStep(client_session=session, url=[get_endpoint])
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]
    assert session.closed
    assert step.client_session is session
    assert step.connector is None


@pytest.mark.asyncio
async def test_supplied_connector_keeps_legacy_close_behavior(get_endpoint: URL) -> None:
    connector = TCPConnector(limit=2)
    step = AsyncHttpStep(connector=connector, url=[get_endpoint])
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]
    assert connector.closed
    assert step.connector is connector
    assert step.client_session is None


@pytest.mark.asyncio
async def test_owned_session_cancelled_during_error_drain(monkeypatch, get_endpoint: URL) -> None:
    step = AsyncHttpStep(url=[get_endpoint, get_endpoint.with_query(index=1)])
    started = asyncio.Event()
    draining = asyncio.Event()
    resources = []
    children = []
    original_request = AsyncHttpStep.request

    async def controlled_request(self, method, url, **kwargs):
        children.append(asyncio.current_task())
        if url.query:
            await started.wait()
            raise ValueError("synthetic request error")
        resources.extend([self.client_session, self.connector])
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(AsyncHttpStep, "request", controlled_request)
    operation = asyncio.create_task(step.get())
    try:
        await asyncio.wait_for(draining.wait(), timeout=5)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert all(child.done() for child in children)
        assert all(resource.closed for resource in resources)
        assert step.client_session is None
        assert step.connector is None
    finally:
        # Release synthetic tasks and resources even if an assertion fails.
        operation.cancel()
        for child in children:
            child.cancel()
        await asyncio.gather(operation, *children, return_exceptions=True)
        for resource in resources:
            await resource.close()

    monkeypatch.setattr(AsyncHttpStep, "request", original_request)
    step.url = [get_endpoint]
    assert await step.get() == [({"url": str(get_endpoint)}, get_endpoint)]


@pytest.fixture
def header_http_server():
    """Record synthetic headers at a loopback server without echoing them."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            code = 404 if self.path == "/error" or (self.path == "/retry" and len(received) == 1) else 200
            self.send_response(code)
            self.send_header("Content-Type", "text/plain" if self.path == "/text" else "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield URL(f"http://127.0.0.1:{server.server_port}"), received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def capture_async_logs():
    """Capture full class logger records, independently of pytest's log threshold."""

    @contextmanager
    def capture(step):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        logger = step.log
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            yield stream
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    return capture


def test_headers_wire_copy_preserves_secret_values():
    sentinel = "SYNTHETIC_HEADER_COPY_e042"
    step = AsyncHttpStep(headers={"Authorization": SecretStr(sentinel), "X-Api-Key": "SYNTHETIC_RAW_KEY_e042"})
    first = step.get_headers()
    second = step.get_headers()
    assert first == second == {"Authorization": sentinel, "X-Api-Key": "SYNTHETIC_RAW_KEY_e042"}
    assert first is not second
    assert isinstance(step.headers["Authorization"], SecretStr)
    assert step.headers["Authorization"].get_secret_value() == sentinel
    first["Authorization"] = "changed wire copy"
    assert step.get_headers()["Authorization"] == sentinel


def test_empty_headers_wire_copy_is_independent():
    step = AsyncHttpStep()
    first = step.get_headers()
    second = step.get_headers()
    assert first == second == {}
    assert first is not second
    assert first is not step.headers
    first["X-New"] = "wire-only"
    assert step.headers == {}


@pytest.mark.parametrize("field_name", ["headers", "header"])
def test_raw_header_values_are_excluded_from_representations(field_name):
    sentinel = "SYNTHETIC_RAW_KEY_REPR_b8a1"
    step = AsyncHttpStep(**{field_name: {"X-Api-Key": sentinel}})
    assert step.get_headers() == {"X-Api-Key": sentinel}
    assert sentinel not in repr(step)
    assert sentinel not in step.__repr_str__(" ")
    assert "headers" not in step.model_dump()


def test_header_secrets_survive_repeated_execute(header_http_server, capture_async_logs):
    endpoint, received = header_http_server
    sentinel = "SYNTHETIC_HEADER_SUCCESS_203f"
    raw_key = "SYNTHETIC_RAW_KEY_SUCCESS_203f"
    step = AsyncHttpStep(urls=[endpoint], headers={"Authorization": SecretStr(sentinel), "X-Api-Key": raw_key})
    with capture_async_logs(step) as logs:
        for _ in range(2):
            assert step.execute().responses_urls == [({}, endpoint)]
            assert isinstance(step.headers["Authorization"], SecretStr)
            assert step.headers["Authorization"].get_secret_value() == sentinel
            assert step.client_session is None
            assert step.connector is None
    assert [headers["Authorization"] for headers in received] == [sentinel, sentinel]
    assert [headers["X-Api-Key"] for headers in received] == [raw_key, raw_key]
    assert "DEBUG Step Input:" in logs.getvalue()
    assert "DEBUG Step Output:" in logs.getvalue()
    assert "Attempt 1 out of" in logs.getvalue()
    assert sentinel not in logs.getvalue()
    assert raw_key not in logs.getvalue()
    assert sentinel not in repr(step)


@pytest.mark.parametrize("path, error_class", [("/error", ClientResponseError), ("/text", ContentTypeError)])
def test_response_error_repr_and_logs_omit_headers(header_http_server, capture_async_logs, path, error_class):
    endpoint, received = header_http_server
    sentinel = "SYNTHETIC_HEADER_ERROR_27fa"
    raw_key = "SYNTHETIC_RAW_ERROR_27fa"
    step = AsyncHttpStep(
        urls=[endpoint / path.lstrip("/")],
        headers={"Authorization": SecretStr(sentinel), "X-Api-Key": raw_key},
        retry_options=ExponentialRetry(attempts=1),
    )
    with capture_async_logs(step) as logs:
        with pytest.raises(error_class) as raised:
            step.execute()
        error = raised.value
        assert type(error) is error_class
        assert error.status == (404 if path == "/error" else 200)
        assert error.request_info.headers["Authorization"] == sentinel
        assert error.request_info.headers["X-Api-Key"] == raw_key
        assert error.request_info.url == step.url[0]
        assert error.args == (error.request_info, error.history)
        step.log.error("Request failed: %r", error)
        step.log.error("Request failed: %s", error)
        for representation in (repr(error), str(error), repr(error.args), repr(step)):
            assert sentinel not in representation
            assert raw_key not in representation
        assert isinstance(step.headers["Authorization"], SecretStr)
        assert step.headers["Authorization"].get_secret_value() == sentinel
        assert step.client_session is None
        assert step.connector is None
        step.url = [endpoint]
        assert step.execute().responses_urls == [({}, endpoint)]
    assert [headers["Authorization"] for headers in received] == [sentinel, sentinel]
    assert "ERROR Error while running step:" in logs.getvalue()
    assert "ERROR Request failed:" in logs.getvalue()
    assert sentinel not in logs.getvalue()
    assert raw_key not in logs.getvalue()


def test_retry_response_error_logs_omit_header_details(header_http_server, capture_async_logs):
    endpoint, received = header_http_server
    sentinel = "SYNTHETIC_HEADER_RETRY_f3b5"
    step = AsyncHttpStep(
        urls=[endpoint / "retry"],
        headers={"Authorization": SecretStr(sentinel)},
        retry_options=ExponentialRetry(attempts=2, start_timeout=0, exceptions={ClientResponseError}),
    )
    with capture_async_logs(step) as logs:
        assert step.execute().responses_urls == [({}, endpoint / "retry")]
    assert [headers["Authorization"] for headers in received] == [sentinel, sentinel]
    assert "Attempt 1 out of 2" in logs.getvalue()
    assert "Attempt 2 out of 2" in logs.getvalue()
    assert sentinel not in logs.getvalue()
    assert "retry event" in logs.getvalue()
    assert isinstance(step.headers["Authorization"], SecretStr)


def test_connection_error_logs_omit_headers(capture_async_logs):
    sentinel = "SYNTHETIC_HEADER_CONNECT_e12a"
    # Reserving a port without listening makes the failure deterministic.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        step = AsyncHttpStep(
            urls=[URL(f"http://127.0.0.1:{sock.getsockname()[1]}")],
            headers={"Authorization": SecretStr(sentinel)},
            retry_options=ExponentialRetry(attempts=1),
        )
        with capture_async_logs(step) as logs:
            with pytest.raises(ClientConnectorError):
                step.execute()
    assert "ERROR Error while running step:" in logs.getvalue()
    assert sentinel not in logs.getvalue()
    assert isinstance(step.headers["Authorization"], SecretStr)
    assert step.client_session is None
    assert step.connector is None


@pytest.mark.asyncio
async def test_cancelled_secret_request_keeps_headers_and_can_retry(
    monkeypatch, header_http_server, capture_async_logs
):
    endpoint, received = header_http_server
    sentinel = "SYNTHETIC_HEADER_CANCEL_197c"
    step = AsyncHttpStep(urls=[endpoint], headers={"Authorization": SecretStr(sentinel)})
    started = asyncio.Event()
    resources = []
    wire_headers = []
    original_request = AsyncHttpStep.request

    async def blocked_request(self, method, url, **kwargs):
        resources.extend([self.client_session, self.connector])
        wire_headers.append(kwargs["headers"])
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncHttpStep, "request", blocked_request)
    with capture_async_logs(step) as logs:
        operation = asyncio.create_task(step.get())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert wire_headers == [{"Authorization": sentinel}]
            assert all(resource.closed for resource in resources)
            assert step.client_session is None
            assert step.connector is None
            assert isinstance(step.headers["Authorization"], SecretStr)
            assert sentinel not in repr(step)
            monkeypatch.setattr(AsyncHttpStep, "request", original_request)
            assert await step.get() == [({}, endpoint)]
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    assert received[0]["Authorization"] == sentinel
    assert isinstance(step.headers["Authorization"], SecretStr)
    assert sentinel not in logs.getvalue()
