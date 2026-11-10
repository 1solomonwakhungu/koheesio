from typing import Any, ContextManager, Optional, Union
from enum import Enum
import os
from pathlib import PurePath
from urllib.parse import urlsplit

from requests import PreparedRequest, Response, Session
from requests.exceptions import RequestException, TooManyRedirects
from tableauserverclient import (
    DatasourceItem,
    PersonalAccessTokenAuth,
    ProjectItem,
    TableauAuth,
)
from tableauserverclient.server.pager import Pager
from tableauserverclient.server.server import Server

from pydantic import Field, FilePath, SecretStr, field_validator

from koheesio.models import model_validator
from koheesio.steps import Step, StepOutput


class _TableauSession(Session):
    """Keep TSC requests on verified HTTPS and stop redirects before TSC can replay credentials."""

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        if urlsplit(request.url or "").scheme != "https":
            raise ValueError("Tableau requests require HTTPS.")
        if not kwargs.get("verify", self.verify):
            raise ValueError("Tableau TLS certificate verification cannot be disabled.")
        kwargs["allow_redirects"] = False
        try:
            response = super().send(request, **kwargs)
        except RequestException as error:
            # TSC logs transport exception text. Do not expose request bodies, headers,
            # proxy credentials or arbitrary text supplied by a transport adapter.
            raise type(error)("Tableau HTTPS transport failed; check certificate trust and connectivity.") from None
        if 300 <= response.status_code < 400:
            response.close()
            # TSC has its own redirect follower in addition to Requests'. Raising here
            # prevents either implementation from forwarding passwords or session tokens.
            raise TooManyRedirects("Tableau redirects are disabled; configure the final HTTPS URL.")
        return response


class TableauServer(Step):
    """
    Base class for Tableau server interactions. Class provides authentication and project identification functionality.
    """

    url: str = Field(
        default=...,
        alias="url",
        description="HTTPS URL for the Tableau server, without credentials, query, or fragment",
        examples=["https://tableau.my-org.com"],
    )
    ca_bundle: Optional[FilePath] = Field(
        default=None,
        description="Optional PEM CA bundle file. TLS certificate verification is always enabled.",
    )
    server: Optional[Server] = Field(default=None, exclude=True, repr=False)
    user: str = Field(default=..., alias="user", description="Login name for the Tableau user")
    password: SecretStr = Field(default=..., alias="password", description="Password for the Tableau user")
    site_id: str = Field(
        default=...,
        alias="site_id",
        description="Identifier for the Tableau site, as used in the URL: https://tableau.my-org.com/#/site/SITE_ID",
    )
    version: str = Field(
        default="3.14",
        alias="version",
        description="Version of the Tableau server API",
    )
    token_name: Optional[str] = Field(
        default=None,
        alias="token_name",
        description="Name of the Tableau Personal Access Token",
    )
    token_value: Optional[SecretStr] = Field(
        default=None,
        alias="token_value",
        description="Value of the Tableau Personal Access Token",
    )
    project: Optional[str] = Field(
        default=None,
        alias="project",
        description="Name of the project on the Tableau server",
    )
    parent_project: Optional[str] = Field(
        default=None,
        alias="parent_project",
        description="Name of the parent project on the Tableau server, use 'root' for the root project.",
    )
    project_id: Optional[str] = Field(
        default=None,
        alias="project_id",
        description="ID of the project on the Tableau server",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Tableau requires an HTTPS URL without credentials, query, or fragment.")
        # Reject invalid ports before constructing a client or unwrapping credentials.
        _ = parsed.port
        return parsed.geturl().rstrip("/")

    @model_validator(mode="after")
    def validate_project(self) -> "TableauServer":
        """Validate when project and project_id are provided at the same time."""

        if self.project and self.project_id:
            raise ValueError("Both 'project' and 'project_id' parameters cannot be provided at the same time.")

        if not self.project and not self.project_id:
            raise ValueError("Either 'project' or 'project_id' parameters should be provided, none is set")

        return self

    @property
    def auth(self) -> ContextManager:
        """
        Authenticate on the Tableau server.

        Examples
        --------
        ```python
        with self._authenticate():
            self.server.projects.get()
        ```

        Returns
        -------
        ContextManager for TableauAuth or PersonalAccessTokenAuth authorization object
        """
        # Assignment validation is disabled on Step, so recheck mutable URL configuration.
        url = self.validate_url(self.url)

        tableau_auth: Union[TableauAuth, PersonalAccessTokenAuth]
        tableau_auth = TableauAuth(username=self.user, password=self.password.get_secret_value(), site_id=self.site_id)

        if self.token_name and self.token_value:
            self.log.info(
                "Token details provided, this will take precedence over username and password authentication."
            )
            tableau_auth = PersonalAccessTokenAuth(
                token_name=self.token_name,
                personal_access_token=self.token_value.get_secret_value(),
                site_id=self.site_id,
            )

        self.server = Server(url, session_factory=_TableauSession)
        self.server.version = self.version
        self.server.add_http_options({"verify": str(self.ca_bundle) if self.ca_bundle else True})

        return self.server.auth.sign_in(tableau_auth)

    @property
    def working_project(self) -> Union[ProjectItem, None]:
        """
        Identify working project by using `project` and `parent_project` (if necessary) class properties.
        The goal is to uniquely identify specific project on the server. If multiple projects have the same
        name, the `parent_project` attribute of the TableauServer is required.

        Notes
        -----
        Set `parent_project` value to 'root' if the project is located in the root directory.

        If `id` of the project is known, it can be used in `project_id` parameter, then the detection of the working
        project using the `project` and `parent_project` attributes is skipped.

        Returns
        -------
        ProjectItem object representing the working project
        """

        with self.auth:
            all_projects = Pager(self.server.projects)
            parent, lim_p = None, []

            for project in all_projects:
                if project.id == self.project_id:
                    lim_p = [project]
                    self.log.info(f"\nProject ID provided directly:\n\tName: {lim_p[0].name}\n\tID: {lim_p[0].id}")
                    break

                # Identify parent project
                if project.name.strip() == self.parent_project and not self.project_id:
                    parent = project
                    self.log.info(f"\nParent project identified:\n\tName: {parent.name}\n\tID: {parent.id}")

                # Identify project(s)
                if project.name.strip() == self.project and not self.project_id:
                    lim_p.append(project)

            # Further filter the list of projects by parent project id
            if self.parent_project == "root" and not self.project_id:
                lim_p = [p for p in lim_p if not p.parent_id]
            elif self.parent_project and parent and not self.project_id:
                lim_p = [p for p in lim_p if p.parent_id == parent.id]

            if len(lim_p) > 1:
                raise ValueError(
                    "Multiple projects with the same name exist on the server, "
                    "please provide `parent_project` attribute."
                )
            elif len(lim_p) == 0:
                raise ValueError("Working project could not be identified.")
            else:
                self.log.info(f"\nWorking project identified:\n\tName: {lim_p[0].name}\n\tID: {lim_p[0].id}")
                return lim_p[0]

    def execute(self) -> None:
        raise NotImplementedError("Method `execute` must be implemented in the subclass.")


class TableauHyperPublishMode(str, Enum):
    """
    Publishing modes for the TableauHyperPublisher.
    """

    APPEND = Server.PublishMode.Append
    OVERWRITE = Server.PublishMode.Overwrite


class TableauHyperPublisher(TableauServer):
    """
    Publish the given Hyper file to the Tableau server. Hyper file will be treated by Tableau server as a datasource.
    """

    datasource_name: str = Field(default=..., description="Name of the datasource to publish")
    hyper_path: PurePath = Field(default=..., description="Path to Hyper file")
    publish_mode: TableauHyperPublishMode = Field(
        default=TableauHyperPublishMode.OVERWRITE,
        description="Publish mode for the Hyper file",
    )

    class Output(StepOutput):
        """
        Output class for TableauHyperPublisher
        """

        datasource_item: DatasourceItem = Field(
            default=..., description="DatasourceItem object representing the published datasource"
        )

    def execute(self) -> None:
        # Ensure that the Hyper File exists
        if not os.path.isfile(self.hyper_path):
            raise FileNotFoundError(f"Hyper file not found at: {self.hyper_path.as_posix()}")

        with self.auth:
            # Finally, publish the Hyper File to the Tableau server
            self.log.info(f'Publishing Hyper File located at: "{self.hyper_path.as_posix()}"')
            self.log.debug(f"Create mode: {self.publish_mode}")

            datasource_item = self.server.datasources.publish(
                datasource_item=DatasourceItem(project_id=str(self.working_project.id), name=self.datasource_name),
                file=self.hyper_path.as_posix(),
                mode=self.publish_mode,
            )
            self.log.info(f"Published datasource to Tableau server with the id: {datasource_item.id}")

            self.output.datasource_item = datasource_item

    def publish(self) -> None:
        self.execute()
