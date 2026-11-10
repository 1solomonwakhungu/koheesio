from typing import Any
import logging
from urllib.parse import urlsplit

import pytest
from requests import Request, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ReadTimeout, SSLError, TooManyRedirects
from tableauserverclient import DatasourceItem
from tableauserverclient.server.endpoint.projects_endpoint import Projects
import urllib3

from pydantic import ValidationError

from koheesio.integrations.spark.tableau.server import TableauHyperPublisher, TableauServer


class TestTableauServer:
    @pytest.fixture(autouse=False)
    def server(self, mocker):
        __server = mocker.patch("koheesio.integrations.spark.tableau.server.Server")
        __mock_server = __server.return_value
        # Pager distinguishes Endpoint instances from callables; an unspecced MagicMock
        # is callable and is therefore treated as the page fetcher on current TSC.
        __mock_server.projects = mocker.MagicMock(spec=Projects)

        # Mocking various returns from the Tableau server
        def create_mock_object(name_prefix: str, object_id: int, spec: Any = None, project_id: int = None):
            obj = mocker.MagicMock() if not spec else mocker.MagicMock(spec=spec)
            obj.id = f"{object_id}"
            obj.name = f"{name_prefix}-{object_id}"
            obj.project_id = f"{project_id}" if project_id else None
            return obj

        def create_mock_pagination(length: int):
            obj = mocker.MagicMock()
            obj.total_available = length
            return obj

        # Projects
        mock_projects = []
        mock_projects_pagination = mocker.MagicMock()

        def create_mock_project(name_prefix: str, project_id: int, parent_id: int = None):
            mock_project = mocker.MagicMock()
            mock_project.name = f"{name_prefix}-{project_id}"
            mock_project.id = f"{project_id}" if not parent_id else f"{project_id * parent_id}"
            mock_project.parent_id = f"{parent_id}" if parent_id else None
            return mock_project

        # Parent Projects
        for i in range(1, 3):
            mock_projects.append(create_mock_project(name_prefix="parent-project", project_id=i))
            # Child Projects
            r = range(3, 5) if i == 1 else range(4, 6)
            for ix in r:
                mock_projects.append(create_mock_project(name_prefix="project", project_id=ix, parent_id=i))

        mock_projects_pagination.total_available = len(mock_projects)
        mock_projects_pagination.page_size = len(mock_projects)
        mock_projects_pagination.page_number = 1

        __mock_server.projects.get.return_value = [
            mock_projects,
            mock_projects_pagination,
        ]

        # Data Sources
        mock_ds = create_mock_object("datasource", 1)
        mock_conn = create_mock_object("connection", 1)
        mock_conn.type = "baz"
        mock_ds.connections = [mock_conn]
        __mock_server.datasources.get.return_value = [
            [mock_ds],
            create_mock_pagination(1),
        ]

        __mock_server.datasources.publish.return_value = create_mock_object(
            "published_datasource", 1337, spec=DatasourceItem
        )

        yield TableauServer(
            url="https://tableau.domain.com", user="user", password="pass", site_id="site", project_id="1"
        )

    @pytest.fixture()
    def hyper_file(self, data_path):
        return f"{data_path}/readers/hyper_file/dummy.hyper"

    def test_working_project_w_project_id(self, server):
        server.project_id = "3"
        wp = server.working_project
        assert wp.id == "3" and wp.name == "project-3"

    def test_working_project_w_project_name(self, server):
        server.project_id = None
        server.project = "project-5"
        wp = server.working_project
        assert wp.id == "10" and wp.name == "project-5"

    def test_working_project_w_project_name_and_parent_project(self, server):
        server.project_id = None
        server.project = "project-4"
        server.parent_project = "parent-project-1"
        wp = server.working_project
        assert wp.id == "4" and wp.name == "project-4"

    def test_working_project_w_project_name_and_root(self, server):
        server.project_id = None
        server.project = "parent-project-1"
        server.parent_project = "root"
        wp = server.working_project
        assert wp.id == "1" and wp.name == "parent-project-1"

    def test_working_project_multiple_projects(self, server):
        with pytest.raises(ValueError):
            server.project_id = None
            server.project = "project-4"
            server.working_project

    def test_working_project_unknown(self, server):
        with pytest.raises(ValueError):
            server.project_id = None
            server.project = "project-6"
            server.working_project

    def test_publish_hyper(self, server, hyper_file):
        p = TableauHyperPublisher(
            url="https://tableau.domain.com",
            user="user",
            password="pass",
            site_id="site",
            project_id="1",
            hyper_path=hyper_file,
            datasource_name="published_datasource",
        )
        p.publish()
        assert p.output.datasource_item.id == "1337"


# These tests retain real TSC serialization, authentication and request dispatch.
# Only the Requests adapter is replaced; no Tableau account or network is used.


PASSWORD = "SYNTHETIC_TABLEAU_PASSWORD_704e"
PAT = "SYNTHETIC_TABLEAU_PAT_17f2"
SESSION_TOKEN = "SYNTHETIC_TABLEAU_SESSION_947a"
SIGNIN_XML = (
    '<tsResponse xmlns="http://tableau.com/api">'
    f'<credentials token="{SESSION_TOKEN}"><site id="site" contentUrl="site"/>'
    '<user id="user"/></credentials></tsResponse>'
)
PROJECTS_XML = (
    '<tsResponse xmlns="http://tableau.com/api">'
    '<pagination pageNumber="1" pageSize="100" totalAvailable="1"/>'
    '<projects><project id="project" name="project"/></projects></tsResponse>'
)
DATASOURCE_XML = (
    '<tsResponse xmlns="http://tableau.com/api">'
    '<datasource id="datasource" name="datasource"><project id="project"/></datasource></tsResponse>'
)


@pytest.fixture
def tableau_transport(monkeypatch):
    state = {"requests": [], "override": None}

    def send(adapter, request, **kwargs):
        state["requests"].append((request, kwargs))
        response = Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response.headers["Content-Type"] = "application/xml"
        path = urlsplit(request.url).path
        if path.endswith("/auth/signin"):
            body = SIGNIN_XML
        elif path.endswith("/auth/signout"):
            response.status_code = 204
            body = ""
        elif path.endswith("/projects"):
            body = PROJECTS_XML
        elif path.endswith("/datasources"):
            response.status_code = 201
            body = DATASOURCE_XML
        else:
            raise AssertionError(f"Unexpected Tableau request path: {path}")
        response._content = body.encode()
        response._content_consumed = True
        response.encoding = "utf-8"
        if state["override"]:
            state["override"](request, response)
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return state


@pytest.fixture
def tableau_publisher(tmp_path):
    def create(auth_kind="password", **kwargs):
        path = tmp_path / "synthetic.hyper"
        path.write_bytes(b"synthetic hyper fixture")
        options = dict(
            url="https://tableau.unit-test.invalid",
            user="synthetic-user",
            password=PASSWORD,
            site_id="site",
            project_id="project",
            hyper_path=path,
            datasource_name="datasource",
        )
        if auth_kind == "pat":
            options.update(token_name="synthetic-token-name", token_value=PAT)
        options.update(kwargs)
        return TableauHyperPublisher(**options)

    return create


@pytest.fixture
def capture_tableau_logs(caplog):
    attached = []

    def capture(step):
        # Set the capture level in the call phase: pytest reapplies log_level
        # between fixture setup and the test, which is CRITICAL in this project.
        caplog.set_level(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger="TSC")
        caplog.set_level(logging.DEBUG, logger="tableau")
        logger = step.log
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(caplog.handler)
        attached.append((logger, previous_level))
        return caplog

    yield capture
    for logger, previous_level in attached:
        logger.removeHandler(caplog.handler)
        logger.setLevel(previous_level)


def assert_no_tableau_secrets(logs):
    for secret in (PASSWORD, PAT, SESSION_TOKEN):
        assert secret not in logs.text


@pytest.mark.parametrize("auth_kind", ["password", "pat"])
@pytest.mark.parametrize("custom_ca", [False, True])
def test_verified_tls_and_auth_wire_values(
    tableau_publisher, tableau_transport, tmp_path, mocker, auth_kind, custom_ca
):
    disable_warnings = mocker.spy(urllib3, "disable_warnings")
    options = {}
    if custom_ca:
        ca = tmp_path / "private-ca.pem"
        ca.write_text("synthetic CA fixture; adapter is mocked")
        options["ca_bundle"] = ca
    step = tableau_publisher(auth_kind, **options)
    with step.auth:
        pass
    assert len(tableau_transport["requests"]) == 2
    request, kwargs = tableau_transport["requests"][0]
    assert kwargs["verify"] == (str(ca) if custom_ca else True)
    body = request.body.decode()
    assert (PAT if auth_kind == "pat" else PASSWORD) in body
    if auth_kind == "pat":
        assert PASSWORD not in body
    assert step.password.get_secret_value() == PASSWORD
    if auth_kind == "pat":
        assert step.token_value.get_secret_value() == PAT
    disable_warnings.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://tableau.unit-test.invalid",
        "tableau.unit-test.invalid",
        "https://",
        "https://user:password@tableau.unit-test.invalid",
        "https://tableau.unit-test.invalid?secret=value",
        "https://tableau.unit-test.invalid#site",
        "https://tableau.unit-test.invalid:invalid",
        "https://tableau.unit-test.invalid\n",
    ],
)
def test_invalid_url_rejected_before_client_creation(tableau_publisher, mocker, url):
    server = mocker.patch("koheesio.integrations.spark.tableau.server.Server")
    with pytest.raises(ValidationError):
        tableau_publisher(url=url)
    server.assert_not_called()


def test_mutated_http_url_rejected_before_authentication(tableau_publisher, tableau_transport):
    step = tableau_publisher()
    step.url = "http://tableau.unit-test.invalid"
    with pytest.raises(ValueError, match="HTTPS"):
        _ = step.auth
    assert tableau_transport["requests"] == []


def test_ca_bundle_must_be_an_existing_file(tableau_publisher, tmp_path):
    with pytest.raises(ValidationError):
        tableau_publisher(ca_bundle=tmp_path / "missing.pem")
    with pytest.raises(ValidationError):
        tableau_publisher(ca_bundle=tmp_path)
    with pytest.raises(ValidationError):
        tableau_publisher(ca_bundle=False)


@pytest.mark.parametrize("auth_kind", ["password", "pat"])
def test_publish_wrappers_do_not_log_credentials_or_fetch_server_info(
    tableau_publisher, tableau_transport, capture_tableau_logs, auth_kind
):
    step = tableau_publisher(auth_kind)
    logs = capture_tableau_logs(step)
    step.execute()
    # A second run has an existing TSC object when the Step Input wrapper runs.
    step.execute()
    assert step.output.datasource_item.id == "datasource"
    assert "Start running step" in logs.text
    assert "Step Input:" in logs.text
    assert "Step Output:" in logs.text
    assert "Finished running step" in logs.text
    assert_no_tableau_secrets(logs)
    assert "server=" not in step.__repr_str__(" ")
    assert "server" not in step.model_dump()
    assert all("serverInfo" not in request.url for request, _ in tableau_transport["requests"])
    assert all(kwargs["verify"] is True for _, kwargs in tableau_transport["requests"])


@pytest.mark.parametrize("auth_kind", ["password", "pat"])
@pytest.mark.parametrize("phase", ["/auth/signin", "/datasources"])
@pytest.mark.parametrize("exception_type", [SSLError, ConnectionError, ReadTimeout])
def test_transport_failure_wrappers_keep_exception_type_and_mask_secrets(
    tableau_publisher, tableau_transport, capture_tableau_logs, auth_kind, phase, exception_type
):
    def fail(request, response):
        if urlsplit(request.url).path.endswith(phase):
            raise exception_type(f"Synthetic transport failure: {PASSWORD} {PAT} {SESSION_TOKEN}")

    tableau_transport["override"] = fail
    step = tableau_publisher(auth_kind)
    logs = capture_tableau_logs(step)
    with pytest.raises(exception_type, match="Tableau HTTPS transport failed") as error:
        step.execute()
    assert "Error while running step:" in logs.text
    assert "Step Input:" in logs.text
    assert_no_tableau_secrets(logs)
    for secret in (PASSWORD, PAT, SESSION_TOKEN):
        assert secret not in str(error.value)
    assert error.value.__suppress_context__
    assert all("serverInfo" not in request.url for request, _ in tableau_transport["requests"])


@pytest.mark.parametrize("auth_kind", ["password", "pat"])
@pytest.mark.parametrize("status", [401, 403, 500])
def test_server_failure_logs_mask_secrets_and_preserve_tsc_error_code(
    tableau_publisher, tableau_transport, capture_tableau_logs, auth_kind, status
):
    def fail(request, response):
        response.status_code = status
        response._content = (
            '<tsResponse xmlns="http://tableau.com/api"><error code="403000">'
            f"<summary>Denied</summary><detail>{PASSWORD} {PAT} {SESSION_TOKEN}</detail>"
            "</error></tsResponse>"
        ).encode()

    tableau_transport["override"] = fail
    step = tableau_publisher(auth_kind)
    logs = capture_tableau_logs(step)
    with pytest.raises(Exception) as error:
        step.execute()
    assert error.value.__class__.__module__.startswith("tableauserverclient.")
    assert error.value.code == (500 if status == 500 else "403000")
    assert "Error while running step:" in logs.text
    assert_no_tableau_secrets(logs)
    # TSC exceptions retain the response. Applications must not log their raw text.
    assert PASSWORD in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "target",
    [
        "https://other.unit-test.invalid/collect",
        "http://tableau.unit-test.invalid/collect",
        "https://tableau.unit-test.invalid/elsewhere",
        "/relative-location",
    ],
)
@pytest.mark.parametrize("phase", ["/auth/signin", "/projects"])
def test_redirects_never_replay_credentials_or_session_tokens(
    tableau_publisher, tableau_transport, capture_tableau_logs, status, target, phase
):
    def redirect(request, response):
        if urlsplit(request.url).path.endswith(phase):
            response.status_code = status
            response.headers["Location"] = target

    tableau_transport["override"] = redirect
    step = tableau_publisher("pat")
    logs = capture_tableau_logs(step)
    with pytest.raises(TooManyRedirects, match="configure the final HTTPS URL"):
        step.execute()
    assert_no_tableau_secrets(logs)
    assert all(
        urlsplit(request.url).hostname == "tableau.unit-test.invalid" for request, _ in tableau_transport["requests"]
    )
    assert all(
        not request.url.endswith(("/collect", "/elsewhere", "/relative-location"))
        for request, _ in tableau_transport["requests"]
    )
    if phase == "/auth/signin":
        assert len(tableau_transport["requests"]) == 1
    else:
        request = next(
            request for request, _ in tableau_transport["requests"] if urlsplit(request.url).path.endswith("/projects")
        )
        assert request.headers["X-Tableau-Auth"] == SESSION_TOKEN


@pytest.mark.parametrize("verification", [False, None, "", 0])
def test_session_rejects_insecure_transport_overrides(tableau_publisher, tableau_transport, verification):
    step = tableau_publisher()
    with step.auth:
        before = len(tableau_transport["requests"])
        with pytest.raises(ValueError, match="HTTPS"):
            step.server.session.get("http://tableau.unit-test.invalid")
        with pytest.raises(ValueError, match="cannot be disabled"):
            step.server.session.send(
                step.server.session.prepare_request(Request("GET", "https://tableau.unit-test.invalid")),
                verify=verification,
            )
        assert len(tableau_transport["requests"]) == before


@pytest.mark.parametrize("token_fields", [{"token_name": "name-only"}, {"token_value": PAT}])
def test_incomplete_pat_configuration_preserves_password_fallback(tableau_publisher, tableau_transport, token_fields):
    step = tableau_publisher(**token_fields)
    with step.auth:
        pass
    body = tableau_transport["requests"][0][0].body.decode()
    assert PASSWORD in body
    assert PAT not in body


def test_https_scheme_and_trailing_slash_are_normalized(tableau_publisher, tableau_transport):
    step = tableau_publisher(url="HTTPS://tableau.unit-test.invalid/")
    with step.auth:
        pass
    assert step.url == "https://tableau.unit-test.invalid"
    assert all(request.url.startswith("https://") for request, _ in tableau_transport["requests"])
