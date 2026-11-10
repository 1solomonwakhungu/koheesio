"""Check the published wheel security and runtime support requirements."""

from importlib.metadata import metadata

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
import pytest


def requirement(name, python="3.11", extra=""):
    environment = dict(default_environment(), python_version=python, python_full_version=f"{python}.25", extra=extra)
    matches = [
        req
        for raw in metadata("koheesio").get_all("Requires-Dist", [])
        if (req := Requirement(raw)).name == name and (req.marker is None or req.marker.evaluate(environment))
    ]
    assert matches, f"Missing active requirement for {name} on Python {python}, extra={extra}"
    return SpecifierSet(",".join(str(req.specifier) for req in matches))


@pytest.mark.parametrize("python", ["3.9", "3.10", "3.11", "3.12"])
def test_core_python_support_is_preserved(python):
    assert f"{python}.25" in SpecifierSet(metadata("koheesio")["Requires-Python"])


@pytest.mark.parametrize("python", ["3.9", "3.10", "3.11", "3.12"])
def test_snowflake_requirement_is_not_silently_omitted(python):
    versions = requirement("snowflake-connector-python", python=python, extra="snowflake")
    for excluded in ["3.12.0", "3.18.1", "4.5.0", "4.7.3", "4.8.1", "5.0.0"]:
        assert excluded not in versions
    assert "5.1.0" in versions
    assert "5.2.0" in versions
    assert "6.0.0" not in versions


def test_cryptography_security_floor():
    versions = requirement("cryptography")
    for unsafe in ["42.0.4", "46.0.5", "49.0.0"]:
        assert unsafe not in versions
    assert "50.0.0" in versions


def test_protobuf_security_floor():
    versions = requirement("protobuf", extra="snowflake")
    assert "6.32.1" not in versions
    assert "6.33.4" not in versions
    assert "6.33.5" in versions
    assert "7.0.0" not in versions


def test_pydantic_security_floor():
    versions = requirement("pydantic")
    assert "2.0.0" not in versions
    assert "2.3.0" not in versions
    assert "2.4.0" in versions
