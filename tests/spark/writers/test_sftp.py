import base64
from contextlib import contextmanager
from io import BytesIO
import logging
import os
from pathlib import Path
import socket
from threading import Event, Thread
from types import SimpleNamespace
from unittest import mock

import paramiko
from paramiko import SSHException
import pytest
from pytest_sftpserver.consts import SERVER_KEY_PUBLIC

from pyspark.sql import SparkSession

from koheesio.integrations.spark.sftp import (
    SendCsvToSftp,
    SendJsonToSftp,
    SFTPWriteMode,
    SFTPWriter,
)
from koheesio.spark.writers.buffer import PandasCsvBufferWriter

pytestmark = pytest.mark.spark


@pytest.fixture
def buffer_writer(spark):
    # Create a small Spark DataFrame for testing
    df = spark.createDataFrame([(1, "a"), (2, "b")], ["column1", "column2"])
    return PandasCsvBufferWriter(df=df)


expected_data = "column1,column2\n1,a\n2,b\n"


@pytest.fixture(scope="session")
def sftp_fixture(sftpserver, tmp_path_factory):
    # https://github.com/ulope/pytest-sftpserver/issues/30
    sftpserver.daemon_threads = True
    sftpserver.block_on_close = False
    key_type, key_data, *_ = Path(SERVER_KEY_PUBLIC).read_text(encoding="utf-8").split()
    sftpserver.trusted_host_key = paramiko.RSAKey(data=base64.b64decode(key_data))
    trust = paramiko.HostKeys()
    trust.add(f"[{sftpserver.host}]:{sftpserver.port}", key_type, sftpserver.trusted_host_key)
    sftpserver.known_hosts = tmp_path_factory.mktemp("sftp-trust") / "known_hosts"
    trust.save(str(sftpserver.known_hosts))
    yield sftpserver


@pytest.fixture(scope="function")
def prepare_sftp(sftp_fixture):
    # create empty files since the sftpserver doesn't allow to create and write to file
    with sftp_fixture.serve_content(
        {
            "test_dir": {
                "overwrite": {"test.csv": "existing_data"},
                "append": {"test.csv": "existing_data"},
                "ignore": {"test.csv": "existing_data"},
                "exclusive": {"test.csv": "existing_data"},
                "backup": {"test.csv": "existing_data"},
                "update_diff_data": {"test.csv": "existing_data"},
                "update_same_data": {"test.csv": expected_data},
            }
        }
    ):
        yield


def read_sftp_file(sftp_fixture, file_path):
    data = None
    sftp = None
    transport = None
    try:
        # Create a new transport and connect to the SFTP server
        transport = paramiko.Transport((sftp_fixture.host, sftp_fixture.port))
        transport.connect(hostkey=sftp_fixture.trusted_host_key, username="a", password="b")

        # Create an SFTP client from the transport
        sftp = paramiko.SFTPClient.from_transport(transport)

        # Try to read the test data from the server
        with sftp.open(file_path, "r") as file:
            data = file.read().decode("utf-8")

    except Exception as e:
        print(f"An error occurred: {e}")

    finally:
        # Close the client and transport
        if sftp:
            sftp.close()
        if transport:
            transport.close()

    return data


def test_canary(sftp_fixture, prepare_sftp):
    """sftpserver setup canary test"""
    actual_data = read_sftp_file(sftp_fixture, "/test_dir/overwrite/test.csv")
    assert actual_data == "existing_data"


def test_invalid_write_mode():
    """Test that an error is raised for an invalid write mode"""
    with pytest.raises(ValueError):
        SFTPWriter(
            buffer_writer=PandasCsvBufferWriter(),
            mode="invalid_mode",
            host="localhost",
            port=22,
            username="a",
            password="b",
            path="/test.csv",
        )


def test_connection_failure(sftp_fixture, buffer_writer):
    """Test that an error is raised when the connection to the SFTP server fails"""
    with mock.patch("paramiko.Transport.start_client", side_effect=SSHException("Could not connect")):
        with pytest.raises(SSHException):
            writer = SFTPWriter(
                buffer_writer=buffer_writer,
                host=sftp_fixture.host,
                port=sftp_fixture.port,
                known_hosts=sftp_fixture.known_hosts,
                username="a",
                password="b",
                path="/test.csv",
            )
            writer.execute()


def test_file_read_failure(sftp_fixture, buffer_writer):
    """Test that an error is raised when reading a file from the SFTP server fails"""
    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode="overwrite",
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path="/test.csv",
    )
    buffer_output = buffer_writer.output.read()
    with mock.patch.object(writer.client, "open", side_effect=IOError("Could not read file")):
        with pytest.raises(IOError):
            writer._handle_write_mode("/test.csv", buffer_output)
    writer._close_client()


def test_file_write_failure(sftp_fixture, buffer_writer):
    """Test that an error is raised when writing a file to the SFTP server fails"""
    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path="/test.csv",
    )
    with mock.patch.object(writer, "write_file", side_effect=IOError("Could not write file")):
        with pytest.raises(IOError):
            writer.execute()


def test_execute_overwrite_mode(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/overwrite/test.csv"
    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.OVERWRITE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == expected_data


def test_execute_append_mode(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/append/test.csv"
    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.APPEND,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == "existing_data" + expected_data


def test_execute_ignore_mode_existing_file(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/ignore/test.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.IGNORE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == "existing_data"  # The existing data should not be overwritten


def test_execute_exclusive_mode_existing_file(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/exclusive/test.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.EXCLUSIVE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act & Assert
    with pytest.raises(FileExistsError):
        writer.execute()


def test_execute_exclusive_mode_new_file(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/exclusive/new_file.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.EXCLUSIVE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == expected_data


def test_execute_backup_mode_existing_file(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/backup/test.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.BACKUP,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == expected_data  # The existing data should be overwritten

    # Assert backup file is created
    transport = paramiko.Transport((sftp_fixture.host, sftp_fixture.port))
    transport.connect(hostkey=sftp_fixture.trusted_host_key, username="a", password="b")

    sftp = None
    try:
        # Create an SFTP client from the transport
        sftp = paramiko.SFTPClient.from_transport(transport)

        # List the files in the directory
        files = sftp.listdir("/test_dir/backup")

        # Check if there is a file that matches the backup file pattern
        backup_files = [f for f in files if f.endswith(".bak")]
        assert len(backup_files) == 1

    finally:
        # Close the client and transport
        if sftp:
            sftp.close()
        transport.close()


def test_execute_update_mode_existing_file_same_data(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/update_same_data/test.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.UPDATE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == expected_data  # The existing data should not be overwritten


def test_execute_update_mode_existing_file_different_data(sftp_fixture, prepare_sftp, buffer_writer):
    # SetUp
    path = "/test_dir/update_diff_data/test.csv"

    writer = SFTPWriter(
        buffer_writer=buffer_writer,
        mode=SFTPWriteMode.UPDATE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    # Act
    writer.execute()

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == expected_data  # The existing data should be overwritten


def test_write_method(sftp_fixture, prepare_sftp, spark):
    """Test that the write method works as expected"""
    # SetUp
    path = "/test_dir/overwrite/test.csv"

    writer = SFTPWriter(
        buffer_writer=PandasCsvBufferWriter(),  # We use a PandasCsvBufferWriter with no data and default parameters
        mode=SFTPWriteMode.OVERWRITE,
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
    )

    df = spark.createDataFrame([(1, "a"), (2, "b")], ["column1", "column2"])
    _expected_data = "column1,column2\n1,a\n2,b\n"

    # Act
    writer.write(df)

    # Assert
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == _expected_data


def test_write_mode_passed_as_string():
    """Test that the write mode can be passed as a string"""
    # SetUp
    writer = SFTPWriter(
        buffer_writer=PandasCsvBufferWriter(),
        mode="overwrite",
        host="localhost",
        port=22,
        username="a",
        password="b",
        path="/test.csv",
    )

    # Act & Assert
    assert writer.mode == SFTPWriteMode.OVERWRITE
    assert writer.write_mode == "wb"


def test_path_and_filename_process_correctly():
    """Test that the path and filename are processed correctly"""
    # SetUp
    writer = SFTPWriter(
        buffer_writer=PandasCsvBufferWriter(),
        mode="overwrite",
        host="goodbye",
        port=42,
        path="/test_dir/",
        filename="thanks_for_all_the_fish.csv",  # we use the alias for file_name explicitly
    )

    # Act & Assert
    assert writer.file_name == "thanks_for_all_the_fish.csv"
    assert writer.path.as_posix() == "/test_dir/thanks_for_all_the_fish.csv"


def test_sftp_removed_from_host_name():
    """Test that the SFTP server is removed from the host after the test"""
    # SetUp
    host = "sftp://localhost/"
    writer = SFTPWriter(
        buffer_writer=PandasCsvBufferWriter(),
        host=host,
        port=42,
        path="/test.csv",
    )

    # Assert
    assert writer.host == "localhost"


def test_invalid_path():
    """Test that an error is raised when an invalid path is passed"""
    with pytest.raises(ValueError):
        SFTPWriter(
            buffer_writer=PandasCsvBufferWriter(),
            host="localhost",
            port=22,
            path=123.456,
        )


def test_send_csv_to_sftp(sftp_fixture, prepare_sftp, spark):
    """Test that the write method works as expected"""
    # SetUp
    path = "/test_dir/overwrite/test.csv"
    test_data = [
        ("foo", 1),
        ("bar", 2),
        ("baz", 3),
    ]
    test_schema = ["name", "value"]

    writer = SendCsvToSftp(
        # Parameters for the SFTPWriter part
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
        # Parameters for the PandasCsvBufferWriter part
        sep="\t",
        line_terminator="\r",
        header=False,
        columns=test_schema,
    )

    _df = spark.createDataFrame(test_data, test_schema)
    _expected_data = "foo\t1\rbar\t2\rbaz\t3\r"

    # Act
    writer.write(_df)

    # Assert that the PandasCsvBufferWriter parameters are set correctly
    assert writer.buffer_writer.sep == "\t"
    assert writer.buffer_writer.lineSep == "\r"  # line_terminator is an alias for lineSep
    assert writer.buffer_writer.header is False
    assert writer.buffer_writer.columns == test_schema

    # Assert that the data was written to the SFTP server
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == _expected_data


def test_send_json_to_sftp(sftp_fixture, prepare_sftp, spark):
    """Test that the write method works as expected"""
    # SetUp
    path = "/test_dir/overwrite/test.csv"
    test_data = [
        ("foo", 1),
        ("bar", 2),
        ("baz", 3),
    ]
    test_schema = ["name", "value"]

    writer = SendJsonToSftp(
        # Parameters for the SFTPWriter part
        host=sftp_fixture.host,
        port=sftp_fixture.port,
        known_hosts=sftp_fixture.known_hosts,
        username="a",
        password="b",
        path=path,
        # Parameters for the PandasJsonBufferWriter part
        orient="split",
        columns=test_schema,
    )

    _df = spark.createDataFrame(test_data, test_schema)
    _expected_data = '{"columns":["name","value"],"index":[0,1,2],"data":[["foo",1],["bar",2],["baz",3]]}'

    # Act
    writer.write(_df)

    # Assert that the PandasCsvBufferWriter parameters are set correctly
    assert writer.buffer_writer.orient == "split"
    assert writer.buffer_writer.columns == test_schema
    assert writer.buffer_writer.compression is None

    # Assert that the data was written to the SFTP server
    actual_data = read_sftp_file(sftp_fixture, path)
    assert actual_data == _expected_data


SFTP_TEST_USERNAME = "synthetic-sftp-user"
SFTP_TEST_PASSWORD = "synthetic-sftp-password-38592"


class _RecordingAuth(paramiko.ServerInterface):
    def __init__(self, state):
        self.state = state

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.state.auth_attempts.append("password")
        if self.state.reject_auth:
            return paramiko.AUTH_FAILED
        assert username == SFTP_TEST_USERNAME
        assert password == SFTP_TEST_PASSWORD
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_none(self, username):
        self.state.auth_attempts.append("none")
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.state.auth_attempts.append("publickey")
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        self.state.auth_attempts.append("interactive")
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _MemorySFTP(paramiko.SFTPServerInterface):
    def __init__(self, server, state):
        super().__init__(server)
        self.state = state

    def stat(self, path):
        if path not in self.state.files:
            return paramiko.SFTP_NO_SUCH_FILE
        attrs = paramiko.SFTPAttributes()
        attrs.st_size = len(self.state.files[path])
        return attrs

    def open(self, path, flags, attr):
        state = self.state

        class Handle(paramiko.SFTPHandle):
            def close(self):
                if flags & (os.O_WRONLY | os.O_RDWR):
                    state.files[path] = self.writefile.getvalue()
                return super().close()

        handle = Handle(flags)
        data = b"" if flags & os.O_TRUNC else self.state.files.get(path, b"")
        handle.readfile = handle.writefile = BytesIO(data)
        if flags & os.O_APPEND:
            handle.writefile.seek(0, 2)
        return handle


@pytest.fixture(scope="module")
def synthetic_host_keys():
    """Two independent, ephemeral server keys; no SSH files or agent are consulted."""
    return paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)


@pytest.fixture
def local_sftp_server():
    @contextmanager
    def serve(key, reject_auth=False, port=0):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(0.1)
        state = SimpleNamespace(
            host="127.0.0.1",
            port=listener.getsockname()[1],
            connections=0,
            auth_attempts=[],
            reject_auth=reject_auth,
            transport=None,
            files={},
            errors=[],
            closed=Event(),
        )
        stop = Event()

        def run():
            try:
                while not stop.is_set():
                    try:
                        connection, _ = listener.accept()
                        break
                    except socket.timeout:
                        continue
                else:
                    return
                state.connections += 1
                state.transport = transport = paramiko.Transport(connection)
                transport.set_log_channel("test.sftp.server")
                transport.add_server_key(key)
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _MemorySFTP, state=state)
                transport.start_server(server=_RecordingAuth(state))
                channel = transport.accept(5)
                while channel is not None and transport.is_active() and not stop.wait(0.01):
                    pass
            except (EOFError, paramiko.SSHException, ConnectionResetError, BrokenPipeError):
                # Clients rejecting the host key end the handshake without authentication.
                pass
            except OSError as error:
                if not stop.is_set():
                    state.errors.append(error)
            except Exception as error:
                state.errors.append(error)
            finally:
                if state.transport is not None:
                    state.transport.close()
                state.closed.set()

        thread = Thread(target=run, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            stop.set()
            listener.close()
            if state.transport is not None:
                state.transport.close()
            thread.join(timeout=6)
            assert not thread.is_alive()
            assert not state.errors

    return serve


def _write_known_hosts(path, host, port, key, hashed=False):
    identity = host if port == 22 else f"[{host}]:{port}"
    if hashed:
        identity = paramiko.HostKeys.hash_host(identity)
    trust = paramiko.HostKeys()
    trust.add(identity, key.get_name(), key)
    trust.save(str(path))
    return path


def _security_writer(server, known_hosts=None):
    spark = mock.Mock(spec=SparkSession)
    return SFTPWriter(
        spark=spark,
        buffer_writer=PandasCsvBufferWriter(spark=spark),
        host=server.host,
        port=server.port,
        username=SFTP_TEST_USERNAME,
        password=SFTP_TEST_PASSWORD,
        path="/payload.csv",
        known_hosts=known_hosts,
    )


def test_security_missing_trust_never_connects():
    writer = _security_writer(SimpleNamespace(host="127.0.0.1", port=22))
    with mock.patch("koheesio.integrations.spark.sftp.Transport") as transport:
        with pytest.raises(ValueError, match="known_hosts"):
            _ = writer.client
        transport.assert_not_called()
    assert writer._client is writer._transport is None


@pytest.mark.parametrize("trust_case", ["empty", "different_host", "wrong_port", "bare_host"])
def test_security_unknown_endpoint_never_authenticates(tmp_path, synthetic_host_keys, local_sftp_server, trust_case):
    key, _ = synthetic_host_keys
    with local_sftp_server(key) as server:
        path = tmp_path / "known_hosts"
        if trust_case == "empty":
            path.write_text("")
        else:
            host = "different.invalid" if trust_case == "different_host" else server.host
            port = 22 if trust_case == "bare_host" else server.port + (trust_case == "wrong_port")
            _write_known_hosts(path, host, port, key)
        writer = _security_writer(server, path)
        try:
            with pytest.raises(SSHException, match="trusted key"):
                _ = writer.client
        finally:
            writer._close_client()
        assert server.auth_attempts == []
        assert server.connections == 0
        assert writer._client is writer._transport is None


def test_security_changed_key_closes_before_authentication(tmp_path, synthetic_host_keys, local_sftp_server, caplog):
    trusted_key, changed_key = synthetic_host_keys
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="test.sftp.server")
    with local_sftp_server(changed_key) as server:
        path = _write_known_hosts(tmp_path / "known_hosts", server.host, server.port, trusted_key)
        writer = _security_writer(server, path)
        try:
            with mock.patch.object(writer.buffer_writer, "write", return_value=BytesIO(b"synthetic,payload\n")):
                with pytest.raises(SSHException, match="does not match"):
                    writer.execute()
        finally:
            writer._close_client()
        assert server.closed.wait(3)
        assert server.connections == 1
        assert server.auth_attempts == []
        assert not server.transport.is_active()
        assert writer._client is writer._transport is None
    assert SFTP_TEST_PASSWORD not in caplog.text
    assert SFTP_TEST_USERNAME not in caplog.text


@pytest.mark.parametrize("hashed", [False, True])
def test_security_trusted_key_transfers_data(tmp_path, synthetic_host_keys, local_sftp_server, caplog, hashed):
    key, _ = synthetic_host_keys
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="test.sftp.server")
    with local_sftp_server(key) as server:
        path = _write_known_hosts(tmp_path / "known_hosts", server.host, server.port, key, hashed=hashed)
        writer = _security_writer(server, path)
        try:
            writer.write_file("/payload.csv", BytesIO(b"synthetic,payload\n"))
            with writer.client.open("/payload.csv", "rb") as uploaded:
                assert uploaded.read() == b"synthetic,payload\n"
        finally:
            writer._close_client()
        assert server.closed.wait(3)
        assert server.files["/payload.csv"] == b"synthetic,payload\n"
        assert server.auth_attempts == ["password"]
        assert writer._client is writer._transport is None
    assert SFTP_TEST_PASSWORD not in caplog.text
    assert SFTP_TEST_USERNAME not in caplog.text


def test_security_authentication_failure_closes_transport(tmp_path, synthetic_host_keys, local_sftp_server, caplog):
    key, _ = synthetic_host_keys
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.WARNING, logger="test.sftp.server")
    with local_sftp_server(key, reject_auth=True) as server:
        path = _write_known_hosts(tmp_path / "known_hosts", server.host, server.port, key)
        writer = _security_writer(server, path)
        with pytest.raises(paramiko.AuthenticationException):
            _ = writer.client
        assert server.closed.wait(3)
        assert server.auth_attempts == ["password"]
        assert writer._client is writer._transport is None
    assert SFTP_TEST_PASSWORD not in caplog.text
    assert SFTP_TEST_USERNAME not in caplog.text


@pytest.mark.parametrize("contents", [None, "127.0.0.1 ssh-rsa invalid-base64"])
def test_security_unreadable_or_malformed_trust_fails_before_socket(tmp_path, contents):
    path = tmp_path / "known_hosts"
    if contents is not None:
        path.write_text(contents)
    writer = _security_writer(SimpleNamespace(host="127.0.0.1", port=22), path)
    with mock.patch("koheesio.integrations.spark.sftp.Transport") as transport:
        with pytest.raises((OSError, paramiko.hostkeys.InvalidHostKey)):
            _ = writer.client
        transport.assert_not_called()
    assert writer._client is writer._transport is None


@pytest.mark.parametrize("port", [22, 2222])
def test_security_host_identity_includes_nondefault_port(tmp_path, synthetic_host_keys, port):
    key, _ = synthetic_host_keys
    path = _write_known_hosts(tmp_path / "known_hosts", "example.invalid", port, key)
    writer = _security_writer(SimpleNamespace(host="sftp://example.invalid/", port=port), path)
    with mock.patch("koheesio.integrations.spark.sftp.Transport") as factory:
        factory.return_value.get_remote_server_key.return_value = key
        assert writer.transport is factory.return_value
        factory.assert_called_once_with(("example.invalid", port))
        factory.return_value.auth_password.assert_called_once()
        writer._close_client()
        factory.return_value.close.assert_called_once()


def test_security_cleanup_does_not_connect():
    writer = _security_writer(SimpleNamespace(host="127.0.0.1", port=22))
    with mock.patch("koheesio.integrations.spark.sftp.Transport") as transport:
        writer._close_client()
        writer._close_client()
        transport.assert_not_called()


def test_security_channel_failure_closes_transport(tmp_path, synthetic_host_keys, local_sftp_server):
    key, _ = synthetic_host_keys
    with local_sftp_server(key) as server:
        path = _write_known_hosts(tmp_path / "known_hosts", server.host, server.port, key)
        writer = _security_writer(server, path)
        with mock.patch("koheesio.integrations.spark.sftp.SFTPClient.from_transport", side_effect=EOFError("closed")):
            with pytest.raises(EOFError, match="closed"):
                _ = writer.client
        assert server.closed.wait(3)
        assert server.auth_attempts == ["password"]
        assert writer._client is writer._transport is None


def test_security_transport_closes_even_if_client_close_fails():
    writer = _security_writer(SimpleNamespace(host="127.0.0.1", port=22))
    writer._client = client = mock.Mock()
    writer._transport = transport = mock.Mock()
    client.close.side_effect = OSError("close failed")
    with pytest.raises(OSError, match="close failed"):
        writer._close_client()
    transport.close.assert_called_once()
    assert writer._client is writer._transport is None


def test_security_buffer_failure_closes_existing_connection(tmp_path, synthetic_host_keys, local_sftp_server):
    key, _ = synthetic_host_keys
    with local_sftp_server(key) as server:
        path = _write_known_hosts(tmp_path / "known_hosts", server.host, server.port, key)
        writer = _security_writer(server, path)
        _ = writer.client
        with mock.patch.object(writer.buffer_writer, "write", side_effect=ValueError("buffer failed")):
            with pytest.raises(ValueError, match="buffer failed"):
                writer.execute()
        assert server.closed.wait(3)
        assert writer._client is writer._transport is None


def test_security_rotation_reloads_explicit_trust(tmp_path, synthetic_host_keys, local_sftp_server):
    old_key, new_key = synthetic_host_keys
    with local_sftp_server(old_key) as old_server:
        path = _write_known_hosts(tmp_path / "known_hosts", old_server.host, old_server.port, old_key)
        writer = _security_writer(old_server, path)
        _ = writer.client
        writer._close_client()
        assert old_server.closed.wait(3)
    with local_sftp_server(new_key, port=old_server.port) as new_server:
        _write_known_hosts(path, new_server.host, new_server.port, new_key)
        try:
            writer.write_file("/payload.csv", BytesIO(b"rotated\n"))
        finally:
            writer._close_client()
        assert new_server.closed.wait(3)
        assert new_server.auth_attempts == ["password"]
        assert new_server.files["/payload.csv"] == b"rotated\n"


@pytest.mark.parametrize("error", [SSHException("handshake failed"), KeyboardInterrupt()])
def test_security_handshake_failure_closes_transport(tmp_path, synthetic_host_keys, error):
    key, _ = synthetic_host_keys
    path = _write_known_hosts(tmp_path / "known_hosts", "example.invalid", 22, key)
    writer = _security_writer(SimpleNamespace(host="example.invalid", port=22), path)
    with mock.patch("koheesio.integrations.spark.sftp.Transport") as factory:
        factory.return_value.start_client.side_effect = error
        with pytest.raises(type(error)):
            _ = writer.client
        factory.return_value.auth_password.assert_not_called()
        factory.return_value.close.assert_called_once()
    assert writer._client is writer._transport is None
