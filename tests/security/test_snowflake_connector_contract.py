"""Exercise installed connector parameter handling before its authentication boundary.

Run these tests without network access. Only authentication, query execution and
close are replaced; the connector's actual constructor and parameter conversion run.
"""

import gc
import sys
from unittest.mock import MagicMock
import weakref

import pytest

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="Snowflake extra requires Python 3.11+")


@pytest.fixture
def connection_boundary(monkeypatch, tmp_path):
    config_dir = tmp_path / "snowflake"
    config_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SNOWFLAKE_HOME", str(config_dir))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("GCE_METADATA_DISABLED", "true")
    monkeypatch.setenv("GCE_METADATA_HOST", "127.0.0.1")
    pytest.importorskip("snowflake.connector")
    from snowflake.connector.connection import Connection

    opened = []
    closed = []
    executed = []

    def connect(connection):
        opened.append(connection)

    def close(connection, *args, **kwargs):
        # A prior test's delayed finalizer can reach this class-wide replacement.
        if any(connection is current for current in opened):
            closed.append(connection)

    def execute(connection, sql):
        executed.append(sql)
        return [MagicMock(fetchall=lambda: [("first",)]), MagicMock(fetchall=lambda: [("second",)])]

    monkeypatch.setattr(Connection, "_connect", connect)
    monkeypatch.setattr(Connection, "close", close)
    return Connection, opened, closed, executed, execute


def step(**overrides):
    from koheesio.integrations.snowflake import SnowflakeRunQueryPython

    fields = dict(
        url="example.snowflakecomputing.com",
        account="example",
        user="synthetic_user",
        password="SYNTHETIC_ONLY_NOT_A_CREDENTIAL",
        database="SYNTHETIC_DB",
        schema="PUBLIC",
        role="SYNTHETIC_ROLE",
        warehouse="SYNTHETIC_WH",
        query="SELECT 1; SELECT 2",
    )
    fields.update(overrides)
    return SnowflakeRunQueryPython(**fields)


@pytest.mark.parametrize("authenticator", [None, "snowflake", "externalbrowser"])
def test_installed_connector_preserves_connection_fields(connection_boundary, authenticator):
    _, opened, closed, _, _ = connection_boundary
    instance = step(authenticator=authenticator)
    with instance.conn as connection:
        assert opened == [connection]
        config = connection.config
        assert config.account == "example"
        assert config.user == "synthetic_user"
        assert config.password == "SYNTHETIC_ONLY_NOT_A_CREDENTIAL"
        assert config.database == "SYNTHETIC_DB"
        assert config.schema == "PUBLIC"
        assert config.role == "SYNTHETIC_ROLE"
        assert config.warehouse == "SYNTHETIC_WH"
        if authenticator is not None:
            assert config.authenticator.lower() == authenticator.lower()
    assert closed == [connection]


def test_installed_connector_preserves_private_key(connection_boundary):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    instance = step(password=None, private_key=pem, authenticator="SNOWFLAKE_JWT")
    with instance.conn as connection:
        assert connection.config.private_key is not None
        assert connection.config.password is None
        assert connection.config.authenticator.upper() == "SNOWFLAKE_JWT"


def test_installed_connector_fetches_each_cursor_and_closes(connection_boundary, monkeypatch):
    connection_class, opened, closed, executed, execute = connection_boundary
    monkeypatch.setattr(connection_class, "execute_string", execute)
    instance = step()
    instance.execute()
    assert executed == ["SELECT 1; SELECT 2"]
    assert instance.output.results == [("first",), ("second",)]
    assert closed == opened


def test_installed_connector_closes_when_query_raises(connection_boundary, monkeypatch):
    from snowflake.connector.errors import ProgrammingError

    connection_class, opened, closed, _, _ = connection_boundary
    error = ProgrammingError(msg="synthetic query rejection", errno=1003, sqlstate="42000")

    def fail(connection, sql):
        raise error

    monkeypatch.setattr(connection_class, "execute_string", fail)
    with pytest.raises(ProgrammingError) as raised:
        step().execute()
    assert raised.value is error
    assert raised.value.errno == 1003
    assert closed == opened


def test_close_tracking_ignores_delayed_finalizers_from_other_connections(connection_boundary):
    connection_class, opened, closed, _, _ = connection_boundary
    previous = object.__new__(connection_class)
    previous.auto_cleanup = True
    previous.is_closed = lambda: False
    previous.cycle = previous
    previous_ref = weakref.ref(previous)
    del previous

    gc.collect()

    assert previous_ref() is None
    assert closed == []
    with step().conn as connection:
        assert opened == [connection]
    assert closed == [connection]
