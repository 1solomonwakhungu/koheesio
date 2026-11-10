from typing import Generator
from io import StringIO
import logging

import pytest
from requests_mock.mocker import Mocker

from koheesio.logger import LoggingFactory
from koheesio.models import SecretStr
from koheesio.sso import okta as o

log = LoggingFactory.get_logger(name="test_download_file", inherit_from_koheesio=True)


@pytest.fixture(scope="function")
def requests_mocker() -> Generator[Mocker, None, None]:
    """Requests mocker fixture"""
    with Mocker() as rm:
        yield rm


# pylint: disable=assignment-from-no-return, redefined-outer-name
class TestOktaToken:
    """Tests for OktaAccessToken class"""

    url = "https://host.okta.com/oauth2/auth/v1/token"
    ot = o.OktaAccessToken(
        url=url,
        client_id="client",
        client_secret=SecretStr("secret"),
    )
    default_options = {
        "headers": {},
        "data": {"grant_type": "client_credentials"},
        "url": "url",
        "timeout": 3,
    }

    def test_okta_token_w_token(self, requests_mocker: Mocker) -> None:
        """Test OktaAccessToken with token"""
        # Arrange
        requests_mocker.post(self.url, json={"access_token": "bar"}, status_code=int(200))
        # Act
        output = self.ot.execute()
        # Assert
        assert output.token.get_secret_value() == "bar"

    def test_okta_token_wo_token(self, requests_mocker: Mocker) -> None:
        """Test OktaAccessToken without token"""
        requests_mocker.post(self.url, json={"foo": "bar"}, status_code=int(200))
        with pytest.raises(ValueError):
            self.ot.execute()

    def test_okta_token_non_200(self, requests_mocker: Mocker) -> None:
        """Test OktaAccessToken with non-200 response"""
        requests_mocker.post(self.url, status_code=int(404))
        with pytest.raises(o.HTTPError):
            self.ot.execute()

    def test_wo_extra_params(self) -> None:
        """Test OktaAccessToken without extra params"""
        oat = o.OktaAccessToken(url="url", client_id="client", client_secret=SecretStr("secret"))
        actual = oat.get_options()
        assert actual == {"auth": ("client", "secret"), **self.default_options}

    def test_w_extra_params(self) -> None:
        """Test OktaAccessToken with extra params"""
        oat = o.OktaAccessToken(url="url", client_id="client", client_secret=SecretStr("secret"), params={"foo": "bar"})
        assert oat.get_options() == {"foo": "bar", "auth": ("client", "secret"), **self.default_options}

    def test_log_extra_params_secret_happy(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that secret values are masked in logs"""
        # Arrange
        # Koheesio loggers have propagate=False, so caplog's root handler never sees records unless attached directly.
        log.addHandler(caplog.handler)
        try:
            self._assert_secret_masked(caplog)
        finally:
            log.removeHandler(caplog.handler)

    @staticmethod
    def _assert_secret_masked(caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG", logger=log.name):
            secret_val = "secret_value"
            # Act
            oat = o.OktaAccessToken(
                url="url", client_id="client", client_secret=SecretStr(secret_val), params={"foo": "bar"}
            )
            log.debug(f"{oat.get_options() = }")
            log.info(f"{oat.get_options() = }")
            log.warning(f"{oat.get_options() = }")
            log.error(f"{oat.get_options() = }")
            log.critical(f"{oat.get_options() = }")
            # Assert
            assert secret_val not in caplog.text
            assert "*" * len(secret_val) + "(Masked)" in caplog.text

    def test_secrets_not_in_log_unhappy(self, requests_mocker: Mocker, caplog: pytest.FixtureRequest) -> None:
        """Test that secret values are not logged upon errors"""
        # Arrange
        requests_mocker.post(self.url, status_code=int(404))
        with caplog.at_level("DEBUG"):
            secret_val = "unique_secret_value"
            # Act
            oat = o.OktaAccessToken(url=self.url, client_id="client", client_secret=SecretStr(secret_val))
            with pytest.raises(o.HTTPError):
                oat.execute()
            # Assert
            assert secret_val not in caplog.text

    def test_ensure_header_is_blank(self) -> None:
        """Test that the header is blank"""
        assert self.ot.headers == {}


@pytest.fixture
def okta_security_logs():
    """Capture every default logging route with token-discovery filters disabled."""
    names = [
        "",
        LoggingFactory.LOGGER_NAME,
        f"{LoggingFactory.LOGGER_NAME}.OktaAccessToken",
        "requests",
        "urllib3",
        "urllib3.connection",
        "urllib3.connectionpool",
        "urllib3.poolmanager",
        "urllib3.response",
        "urllib3.util.retry",
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


def secure_okta_step():
    step = o.OktaAccessToken(
        url=TestOktaToken.url, client_id="synthetic-client", client_secret=SecretStr("SYNTHETIC_CLIENT_SECRET_94bd")
    )
    # The regression must pass before any token is known, without the late masking filter.
    step.log.filters.clear()
    return step


def test_okta_token_protected_before_assignment(requests_mocker, okta_security_logs):
    secret = "SYNTHETIC_OKTA_SUCCESS_TOKEN_faa1"
    nested_secret = "SYNTHETIC_OKTA_NESTED_SECRET_b19c"
    payload = {"access_token": secret, "nested": [{"unrelated": nested_secret}]}
    requests_mocker.post(TestOktaToken.url, json=payload)
    step = secure_okta_step()
    token_states = []

    class AssignmentObserver(logging.Handler):
        def emit(self, record):
            token_states.append((record.getMessage(), step.output.token))

    observer = AssignmentObserver()
    step.log.addHandler(observer)
    try:
        output = step.execute()
    finally:
        step.log.removeHandler(observer)

    assert isinstance(output.token, SecretStr)
    assert output.token.get_secret_value() == secret
    assert output.response_json == payload
    assert output.response_raw.json() == payload
    assert any("Received response" in message and token is None for message, token in token_states)
    assert any("Step Output:" in message for message, _ in token_states)
    for value in (secret, nested_secret, "SYNTHETIC_CLIENT_SECRET_94bd"):
        assert value not in okta_security_logs.text
        assert value not in repr(step)
        assert value not in repr(output)
        assert value not in step.repr_json()
        assert value not in step.repr_yaml()


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": {"token": "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"}},
        [{"access_token": "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"}],
        {"access_token": {"token": "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"}},
        {"access_token": ["SYNTHETIC_OKTA_BAD_PAYLOAD_c420"]},
        {"access_token": 123, "other": "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"},
        {"access_token": "", "other": "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"},
        None,
    ],
)
def test_okta_invalid_token_response_has_safe_error(payload, requests_mocker, okta_security_logs):
    import traceback

    requests_mocker.post(TestOktaToken.url, json=payload)
    step = secure_okta_step()
    with pytest.raises(ValueError) as caught:
        with step:
            step.execute()
    secret = "SYNTHETIC_OKTA_BAD_PAYLOAD_c420"
    assert secret not in str(caught.value)
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert secret not in okta_security_logs.text
    assert secret not in repr(step)
    assert step.output.token is None


@pytest.mark.parametrize("status", [201, 400, 500])
def test_okta_non_200_response_has_safe_error(status, requests_mocker, okta_security_logs):
    secret = "SYNTHETIC_OKTA_FAILED_RESPONSE_adb4"
    requests_mocker.post(TestOktaToken.url, json={"access_token": secret}, status_code=status, reason=secret)
    step = secure_okta_step()
    with pytest.raises(o.HTTPError) as caught:
        with step:
            step.execute()
    assert caught.value.response.json()["access_token"] == secret
    assert secret not in str(caught.value)
    assert secret not in okta_security_logs.text
    assert secret not in repr(step)
    assert step.output.token is None


@pytest.mark.parametrize("second_response", ["malformed", "empty", "missing", "error", "success"])
def test_okta_repeated_request_does_not_reuse_stale_token(second_response, requests_mocker, okta_security_logs):
    old_secret = "SYNTHETIC_OKTA_OLD_TOKEN_20bf"
    new_secret = "SYNTHETIC_OKTA_NEW_TOKEN_65bf"
    step = secure_okta_step()
    requests_mocker.post(TestOktaToken.url, json={"access_token": old_secret})
    step.execute()
    previous_response = step.output.response_raw

    if second_response == "success":
        requests_mocker.post(TestOktaToken.url, json={"access_token": new_secret})
        assert step.execute().token.get_secret_value() == new_secret
    else:
        if second_response == "malformed":
            requests_mocker.post(TestOktaToken.url, text=new_secret)
        elif second_response == "empty":
            requests_mocker.post(TestOktaToken.url, text="")
        elif second_response == "missing":
            requests_mocker.post(TestOktaToken.url, json={"nested": {"token": new_secret}})
        else:
            requests_mocker.post(TestOktaToken.url, json={"access_token": new_secret}, status_code=400)
        with pytest.raises((ValueError, o.HTTPError)):
            with step:
                step.execute()
        assert step.output.token is None
        if second_response == "error":
            assert step.output.response_raw is previous_response
        elif second_response in ("malformed", "empty"):
            assert step.output.response_json is None

    for secret in (old_secret, new_secret):
        assert secret not in okta_security_logs.text
        assert secret not in repr(step)
        assert secret not in repr(step.output)
