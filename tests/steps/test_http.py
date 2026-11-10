from typing import List, Type

import pytest
from requests import HTTPError
from requests.exceptions import JSONDecodeError, RetryError
import responses
from responses import matchers
from responses.registries import OrderedRegistry

from pydantic import ValidationError

from koheesio.logger import LoggingFactory
from koheesio.models import SecretStr
from koheesio.steps.http import (
    HttpDeleteStep,
    HttpGetStep,
    HttpMethod,
    HttpPostStep,
    HttpPutStep,
    HttpStep,
    PaginatedHttpGetStep,
)

BASE_URL = "https://42.koheesio.test"
GET_ENDPOINT = f"{BASE_URL}/get"
POST_ENDPOINT = f"{BASE_URL}/post"
PUT_ENDPOINT = f"{BASE_URL}/put"
DELETE_ENDPOINT = f"{BASE_URL}/delete"
STATUS_404_ENDPOINT = f"{BASE_URL}/status/404"
STATUS_500_ENDPOINT = f"{BASE_URL}/status/500"
STATUS_503_ENDPOINT = f"{BASE_URL}/status/503"

log = LoggingFactory.get_logger(name="test_http", inherit_from_koheesio=True)


@responses.activate
@pytest.mark.parametrize(
    "endpoint,step,method,return_value,expected_status_code",
    [
        pytest.param(
            GET_ENDPOINT,
            HttpGetStep,
            "get",
            {"job_id": "1234"},
            200,
            id="httpGetStep_success",
        ),
        pytest.param(GET_ENDPOINT, HttpGetStep, "get", {"message": "Error Code"}, 400, id="httpGetStep_error"),
        pytest.param(GET_ENDPOINT, HttpGetStep, "get", None, 404, id="httpGetStep_emptyresponse"),
        pytest.param(
            POST_ENDPOINT,
            HttpPostStep,
            "post",
            {"job_id": "1234"},
            200,
            id="httpPostStep_success",
        ),
        pytest.param(POST_ENDPOINT, HttpPostStep, "post", {"message": "Error Code"}, 400, id="httpPosStep_error"),
        pytest.param(
            PUT_ENDPOINT,
            HttpPutStep,
            "put",
            {"detail": "Updated"},
            202,
            id="httpPutStep_success",
        ),
        pytest.param(PUT_ENDPOINT, HttpPutStep, "put", {"message": "Error Code"}, 400, id="httpPutStep_error"),
        pytest.param(
            PUT_ENDPOINT, HttpPutStep, "put", {"message": "Validation Error"}, 422, id="httpPutStep_validationError"
        ),
        pytest.param(
            DELETE_ENDPOINT,
            HttpDeleteStep,
            "delete",
            None,
            204,
            id="httpDeleteStep_success",
        ),
        pytest.param(
            DELETE_ENDPOINT, HttpDeleteStep, "delete", {"message": "Error Code"}, 400, id="httpDeleteStep_error"
        ),
        pytest.param(
            DELETE_ENDPOINT,
            HttpDeleteStep,
            "delete",
            {"message": "Validation Error"},
            422,
            id="httpDeleteStep_validationError",
        ),
    ],
)
def test_http_step(endpoint: str, step: HttpStep, method: str, return_value: dict, expected_status_code: int) -> None:
    """Test basic HTTP methods with success and error responses"""
    responses.add(getattr(responses, method.upper()), endpoint, json=return_value, status=expected_status_code)

    step = step(url=endpoint)

    if expected_status_code not in (200, 202, 204):
        with pytest.raises(HTTPError) as excinfo:
            step.execute()
        assert excinfo.value.response.status_code == expected_status_code
    else:
        response = step.execute()
        assert response.status_code == expected_status_code
        assert response.response_json == return_value


@responses.activate
def test_http_step_with_valid_http_method() -> None:
    """Unit Testing the GET functions with valid method."""
    responses.add(responses.GET, GET_ENDPOINT, status=200)
    step = HttpStep(method="get", url=GET_ENDPOINT)
    step.execute()
    assert step.output.status_code == 200  # type: ignore


@responses.activate
def test_http_step_with_invalid_http_method():
    responses.add(responses.GET, GET_ENDPOINT, status=200)
    # Will be raised during class instantiation
    with pytest.raises(AttributeError):
        HttpStep(method="foo", url=GET_ENDPOINT).execute()


@responses.activate
def test_http_step_request():
    responses.add(responses.PUT, PUT_ENDPOINT, status=200)
    # The default method for HttpStep class is GET, however the method specified in `request` options is PUT and
    # it will override the default
    step = HttpStep(url=PUT_ENDPOINT)
    with step._request(method=HttpMethod.PUT) as response:
        assert response.status_code == 200


EXAMPLE_DIGEST_AUTH = (
    'Digest username="Zaphod", realm="galaxy@heartofgold.com", nonce="42answer", uri="/dir/restaurant.html", '
    'response="dontpanic42", opaque="babelfish"'
)


@responses.activate
@pytest.mark.parametrize(
    "params, expected",
    [
        pytest.param(
            dict(url=GET_ENDPOINT, headers={"Authorization": "Bearer token", "Content-Type": "application/json"}),
            "Bearer token",
            id="bearer_token_in_headers",
        ),
        pytest.param(
            dict(url=GET_ENDPOINT, headers={"Content-Type": "application/json"}, auth_header="Bearer token"),
            "Bearer token",
            id="bearer_token_through_auth_header",
        ),
        pytest.param(
            dict(url=GET_ENDPOINT, auth_header=EXAMPLE_DIGEST_AUTH),
            EXAMPLE_DIGEST_AUTH,
            id="digest_auth_with_nonce",
        ),
    ],
)
def test_get_headers(params: dict, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    """
    Authorization headers are being converted into SecretStr under the hood to avoid dumping any
    sensitive content into logs. However, when calling the `get_headers` method, the SecretStr is being
    converted back to string, otherwise sensitive info would have looked like '**********'.
    """
    # Arrange and Act
    responses.add(responses.GET, params["url"], status=200)  # Mock the request to be always successful
    step = HttpStep(**params)
    caplog.set_level("DEBUG", logger=step.log.name)
    auth = step.headers.get("Authorization")
    step.execute()

    # Check that the token doesn't accidentally leak in the logs
    assert len(caplog.records) > 1, "No logs were generated"
    for record in caplog.records:
        assert expected not in record.message

    # Ensure that the Authorization header is properly parsed to a SecretStr
    assert auth is not None, "Authorization header is missing"
    assert isinstance(auth, SecretStr)
    assert str(auth) == "**********"
    assert auth.get_secret_value() == expected

    # Ensure that the Content-Type header is properly parsed while not being a SecretStr
    assert step.headers["Content-Type"] == "application/json"


@responses.activate
@pytest.mark.parametrize(
    "max_retries, endpoint, status_code, expected_count, error_type",
    [
        pytest.param(1, STATUS_503_ENDPOINT, 503, 2, RetryError, id="max_retries_2_503"),
        pytest.param(5, STATUS_503_ENDPOINT, 502, 6, RetryError, id="max_retries_6_502"),
        # Only 502, 503, 504 are included in the retry list
        pytest.param(3, STATUS_404_ENDPOINT, 404, 1, HTTPError, id="max_retries_1_404"),
    ],
)
def test_max_retries(
    max_retries: int, endpoint: str, status_code: int, expected_count: int, error_type: Type[Exception]
) -> None:
    """Test retry behavior for different status codes"""
    request_count = 0

    def request_callback(request):
        nonlocal request_count
        request_count += 1
        return (status_code, {}, "Error")

    responses.add_callback(responses.GET, endpoint, callback=request_callback)

    step = HttpGetStep(url=endpoint, max_retries=max_retries)

    with pytest.raises(error_type):
        step.execute()

    assert request_count == expected_count


@responses.activate
@pytest.mark.parametrize("payload", [[{"id": 1}], {"id": 1}])
def test_paginated_http_get_without_pagination(payload):
    responses.get(
        GET_ENDPOINT,
        json=payload,
        match=[matchers.request_kwargs_matcher({"timeout": 7})],
    )
    step = PaginatedHttpGetStep(url=GET_ENDPOINT, headers={"X-Test": "pagination"}, timeout=7)

    result = step.execute()

    assert result.response_json == [{"id": 1}]
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == GET_ENDPOINT
    assert responses.calls[0].request.headers["X-Test"] == "pagination"
    assert step.url == GET_ENDPOINT
    assert result.response_raw is None
    assert result.raw_payload is None
    assert result.status_code is None


@responses.activate(registry=OrderedRegistry)
@pytest.mark.parametrize("offset", [1, 3])
@pytest.mark.parametrize("array_payload", [False, True])
def test_paginated_http_get_page_count(offset, array_payload):
    template = f"{GET_ENDPOINT}?page={{page}}&limit={{limit}}&offset={{offset}}"
    expected_urls = [f"{GET_ENDPOINT}?page={page}&limit=10&offset={offset}" for page in range(offset, offset + 2)]
    expected_data = [{"page": page} for page in range(offset, offset + 2)]
    for url, payload in zip(expected_urls, expected_data):
        responses.get(
            url,
            json=[payload] if array_payload else payload,
            match=[matchers.request_kwargs_matcher({"timeout": 7})],
        )
    step = PaginatedHttpGetStep(
        url=template,
        paginate=True,
        pages=2,
        offset=offset,
        limit=10,
        headers={"X-Test": "pagination"},
        timeout=7,
    )

    result = step.execute()

    assert [call.request.url for call in responses.calls] == expected_urls
    assert result.response_json == expected_data
    assert all(call.request.headers["X-Test"] == "pagination" for call in responses.calls)
    assert step.url == template


@responses.activate(registry=OrderedRegistry)
@pytest.mark.parametrize("failure", ["http", "json"])
def test_paginated_http_get_restores_url_after_failure(failure):
    template = f"{GET_ENDPOINT}?page={{page}}"
    responses.get(f"{GET_ENDPOINT}?page=1", json=[{"page": 1}])
    if failure == "http":
        responses.get(f"{GET_ENDPOINT}?page=2", status=500)
        expected_error = HTTPError
    else:
        responses.get(f"{GET_ENDPOINT}?page=2", body="invalid JSON", content_type="application/json")
        expected_error = JSONDecodeError
    step = PaginatedHttpGetStep(url=template, paginate=True, pages=2, max_retries=0)

    with pytest.raises(expected_error):
        step.execute()

    assert step.url == template
    assert [call.request.url for call in responses.calls] == [
        f"{GET_ENDPOINT}?page=1",
        f"{GET_ENDPOINT}?page=2",
    ]

    responses.reset()
    for page in (1, 2):
        responses.get(f"{GET_ENDPOINT}?page={page}", json=[{"page": page}])

    result = step.execute()

    assert result.response_json == [{"page": 1}, {"page": 2}]
    assert [call.request.url for call in responses.calls] == [
        f"{GET_ENDPOINT}?page=1",
        f"{GET_ENDPOINT}?page=2",
    ]
    assert step.url == template


@pytest.fixture
def http_security_logs():
    """Capture direct class loggers, parent/root handlers, and HTTP library loggers."""
    import logging

    names = [
        "",
        LoggingFactory.LOGGER_NAME,
        "requests",
        "urllib3",
        "urllib3.connection",
        "urllib3.connectionpool",
        "urllib3.poolmanager",
        "urllib3.response",
        "urllib3.util.retry",
        *[
            f"{LoggingFactory.LOGGER_NAME}.{name}"
            for name in (
                "HttpStep",
                "HttpGetStep",
                "HttpPostStep",
                "HttpPutStep",
                "HttpDeleteStep",
                "PaginatedHttpGetStep",
                "OktaAccessToken",
            )
        ],
    ]

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.records = []
            self.messages = []

        def emit(self, record):
            self.records.append(record)
            self.messages.append(self.format(record))

        @property
        def text(self):
            return "\n".join(self.messages)

    capture = Capture()
    state = []
    for name in names:
        logger = logging.getLogger(name)
        state.append((logger, logger.level, list(logger.filters)))
        logger.setLevel(logging.DEBUG)
        logger.filters.clear()
        logger.addHandler(capture)
    try:
        yield capture
    finally:
        for logger, level, filters in state:
            logger.setLevel(level)
            logger.filters[:] = filters
            logger.removeHandler(capture)


@responses.activate
@pytest.mark.parametrize("step_type", [HttpStep, HttpGetStep, HttpPostStep, HttpPutStep, HttpDeleteStep])
@pytest.mark.parametrize("entrypoint", ["execute", "run"])
def test_response_secrets_absent_from_http_logs_and_repr(step_type, entrypoint, http_security_logs):
    """Response fields stay accessible without entering any default representation."""
    secret = "SYNTHETIC_HTTP_NESTED_SECRET_940f"
    payload = {"access_token": secret, "nested": [{"token": secret}], "unrelated": secret}
    step = step_type(url=GET_ENDPOINT)
    responses.add(step.method.value.upper(), GET_ENDPOINT, json=payload)

    output = getattr(step, entrypoint)()

    assert output.response_json == payload
    assert output.json_payload == payload
    assert output.raw_payload == output.response_raw.text
    assert output.model_dump()["response_json"] == payload
    representations = [
        repr(step),
        str(step),
        step.__repr_str__(" "),
        step.repr_json(),
        step.repr_json(simple=True),
        step.repr_yaml(),
        step.repr_yaml(simple=True),
        repr(output),
        str(output),
        output.__repr_str__(" "),
    ]
    assert all(secret not in value for value in representations)
    assert secret not in http_security_logs.text
    messages = [record.getMessage() for record in http_security_logs.records]
    if entrypoint == "execute":
        assert any("Step Input:" in message for message in messages)
        assert any("Step Output:" in message for message in messages)
    assert any("status code 200" in message for message in messages)


@responses.activate
@pytest.mark.parametrize("second_response", ["empty", "malformed", "error"])
def test_response_secrets_absent_on_reused_http_step(second_response, http_security_logs):
    """Previous output cannot enter logs when the next request fails or has no JSON."""
    secret = "SYNTHETIC_HTTP_REUSED_SECRET_1873"
    step = HttpGetStep(url=GET_ENDPOINT)
    responses.get(GET_ENDPOINT, json={"nested": {"token": secret}})
    step.execute()
    previous_response = step.output.response_raw
    responses.reset()
    if second_response == "error":
        responses.get(GET_ENDPOINT, status=400, body=secret)
        with pytest.raises(HTTPError):
            with step:
                step.execute()
        # Request failures keep caller-accessible previous output, hidden from representations.
        assert step.output.response_raw is previous_response
    else:
        responses.get(GET_ENDPOINT, body="" if second_response == "empty" else secret)
        output = step.execute()
        assert output.response_json is None
    assert secret not in http_security_logs.text
    assert secret not in repr(step)
    assert secret not in repr(step.output)


@responses.activate
@pytest.mark.parametrize("status", [400, 401, 500])
def test_http_error_preserves_response_without_reflected_message(status, http_security_logs):
    """Requests' exception objects retain their response while their displayed message omits its reason and URL."""
    import traceback

    secret = "SYNTHETIC_HTTP_REASON_SECRET_b915"
    responses.get(GET_ENDPOINT, status=status, body=secret)
    step = HttpGetStep(url=GET_ENDPOINT)
    original_send = step.session.send

    def send(*args, **kwargs):
        response = original_send(*args, **kwargs)
        response.reason = secret
        response.url = f"https://reflected.invalid/{secret}"
        return response

    step.session.send = send
    with pytest.raises(HTTPError) as caught:
        with step:
            step.execute()

    error = caught.value
    assert error.response.text == secret
    assert error.response.reason == secret
    assert error.request is error.response.request
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in "".join(traceback.format_exception(error))
    assert secret not in http_security_logs.text
    assert str(status) in str(error)


@responses.activate
def test_scalar_json_error_has_safe_message(http_security_logs):
    secret = "SYNTHETIC_HTTP_SCALAR_SECRET_9ee2"
    responses.get(GET_ENDPOINT, json=secret)
    with pytest.raises(ValueError):
        with HttpGetStep(url=GET_ENDPOINT) as step:
            step.execute()
    assert secret not in http_security_logs.text
    assert step.output.raw_payload == f'"{secret}"'
    assert step.output.response_json is None


@responses.activate
def test_paginated_response_secrets_remain_available_without_logging(http_security_logs):
    secret = "SYNTHETIC_HTTP_PAGINATED_SECRET_cfe8"
    template = f"{GET_ENDPOINT}?page={{page}}"
    for page in (1, 2):
        responses.get(f"{GET_ENDPOINT}?page={page}", json=[{"token": secret, "page": page}])
    step = PaginatedHttpGetStep(url=template, paginate=True, pages=2)
    output = step.execute()
    assert output.response_json == [{"token": secret, "page": page} for page in (1, 2)]
    assert secret not in http_security_logs.text
    assert secret not in repr(step)
    assert secret not in repr(output)
    assert step.url == template


@responses.activate
def test_request_stream_does_not_read_body_for_logging(http_security_logs, monkeypatch):
    from requests import Response

    def forbidden_text(_response):
        raise AssertionError("Streaming response body was accessed before yielding")

    responses.get(GET_ENDPOINT, body="SYNTHETIC_STREAM_BODY_93a1")
    step = HttpGetStep(url=GET_ENDPOINT)
    monkeypatch.setattr(Response, "text", property(forbidden_text))
    with step._request(stream=True) as response:
        assert response.status_code == 200
    assert "SYNTHETIC_STREAM_BODY_93a1" not in http_security_logs.text


@pytest.fixture
def security_response_server():
    """Serve synthetic responses over loopback to exercise Requests and urllib3 together."""
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    @contextmanager
    def serve(status, reason, body, headers):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status, reason)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/response"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return serve


@pytest.mark.parametrize("failure", ["reason", "retry_after", "retry_exhausted"])
def test_library_response_errors_have_safe_logs(failure, security_response_server, http_security_logs):
    """Exercise actual urllib3 retry/error construction with server-controlled response data."""
    from requests.exceptions import InvalidHeader

    secret = "SYNTHETIC_LIBRARY_RESPONSE_SECRET_d91f"
    status = 400 if failure == "reason" else 503
    headers = {"Retry-After": secret} if failure == "retry_after" else {}
    expected_error = {"reason": HTTPError, "retry_after": InvalidHeader, "retry_exhausted": RetryError}[failure]
    with security_response_server(status, secret, secret.encode(), headers) as url:
        step = HttpGetStep(url=url, max_retries=1, backoff_factor=0)
        with pytest.raises(expected_error) as caught:
            with step:
                step.execute()
    assert secret not in str(caught.value)
    assert secret not in http_security_logs.text
    assert any(record.name == "urllib3.connectionpool" for record in http_security_logs.records)
    assert any("Error while running step:" in record.getMessage() for record in http_security_logs.records)


def test_json_decode_error_message_is_not_logged(http_security_logs):
    import json

    from requests import Response

    secret = "SYNTHETIC_JSON_DECODER_MESSAGE_b142"
    response = Response()
    response.status_code = 200
    response._content = b"not JSON"

    def decode():
        raise json.JSONDecodeError(secret, "", 0)

    response.json = decode
    step = HttpGetStep(url=GET_ENDPOINT)
    step.set_outputs(response)
    assert step.output.response_json is None
    assert step.output.raw_payload == "not JSON"
    assert secret not in http_security_logs.text
    assert any(record.levelname == "ERROR" for record in http_security_logs.records)


@pytest.mark.parametrize("malformed", ["header", "status"])
def test_malformed_wire_response_cannot_escape_library_logs(malformed, http_security_logs):
    import socketserver
    import threading

    from requests.exceptions import ConnectionError

    secret = "SYNTHETIC_MALFORMED_WIRE_RESPONSE_0341"
    if malformed == "header":
        wire = f"HTTP/1.1 200 OK\r\n{secret}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
    else:
        wire = f"{secret}\r\n\r\n".encode()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(8192)
            self.request.sendall(wire)

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        step = HttpGetStep(url=f"http://127.0.0.1:{server.server_address[1]}/", max_retries=1, backoff_factor=0)
        if malformed == "header":
            step.execute()
        else:
            with pytest.raises(ConnectionError):
                with step:
                    step.execute()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert secret not in http_security_logs.text
    library_records = [record for record in http_security_logs.records if record.name.startswith("urllib3.")]
    assert library_records
    # Inspect arguments, exception state, and extra attributes, not only formatted messages.
    assert all(secret not in repr(vars(record)) for record in library_records)


@responses.activate
def test_transport_log_protection_is_scoped_and_restored(http_security_logs):
    import logging
    from threading import Thread

    from requests.exceptions import ConnectionError

    logger = logging.getLogger("urllib3.connectionpool")
    outside = "unrelated-request-diagnostic"
    inside = "SYNTHETIC_TRANSPORT_DIAGNOSTIC_811b"
    responses.get(GET_ENDPOINT, body=ConnectionError(inside))
    step = HttpGetStep(url=GET_ENDPOINT)
    original_request = step.session.request

    def request(*args, **kwargs):
        logger.warning(inside)
        thread = Thread(target=lambda: logger.warning(outside))
        thread.start()
        thread.join(timeout=2)
        return original_request(*args, **kwargs)

    step.session.request = request
    with pytest.raises(ConnectionError):
        step.execute()
    logger.warning(outside)
    assert inside not in http_security_logs.text
    assert outside in http_security_logs.text
    assert sum(record.getMessage() == outside for record in http_security_logs.records) >= 2


def test_okta_loopback_response_has_safe_default_logs(security_response_server, http_security_logs):
    import json

    from koheesio.sso.okta import OktaAccessToken

    secret = "SYNTHETIC_LOOPBACK_OKTA_TOKEN_cd91"
    payload = {"access_token": secret, "nested": [{"unrelated": secret}]}
    with security_response_server(200, "OK", json.dumps(payload).encode(), {}) as url:
        step = OktaAccessToken(url=url, client_id="synthetic-client", client_secret=SecretStr("synthetic-secret"))
        step.log.filters.clear()
        with step:
            output = step.execute()
    assert output.token.get_secret_value() == secret
    assert output.response_json == payload
    assert secret not in http_security_logs.text
    assert any(record.name == step.log.name for record in http_security_logs.records)
    assert any(record.name == "urllib3.connectionpool" for record in http_security_logs.records)


@responses.activate
@pytest.mark.parametrize("inner_failure", [False, True])
def test_transport_logging_nested_requests_restore_outer_context(inner_failure, http_security_logs):
    import logging

    logger = logging.getLogger("urllib3.connectionpool")
    secret = "SYNTHETIC_NESTED_TRANSPORT_SECRET_b2e1"
    outside = "after-nested-request-diagnostic"
    responses.get(GET_ENDPOINT, json={})
    responses.get(POST_ENDPOINT, json={}, status=400 if inner_failure else 200)
    outer = HttpGetStep(url=GET_ENDPOINT)
    inner = HttpGetStep(url=POST_ENDPOINT)
    with outer._request():
        installed_filters = tuple(logger.filters)
        if inner_failure:
            with pytest.raises(HTTPError):
                with inner._request():
                    pass
        else:
            with inner._request():
                logger.warning("response detail: %s", secret)
        assert tuple(logger.filters) == installed_filters
        try:
            raise ValueError(secret)
        except ValueError:
            logger.exception("response detail: %s", secret)
    logger.warning(outside)
    assert secret not in http_security_logs.text
    assert outside in http_security_logs.text
    assert tuple(logger.filters) == installed_filters
