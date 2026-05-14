"""Tests for DockerSandbox initialization (without running Docker)."""

import mimetypes
import time

import pytest


@pytest.fixture(scope="module")
def docker_sandbox():
    """Shared Docker sandbox for TestDockerSandboxEdit class.

    Reduces container creation from 3 times to 1 time.
    """
    pytest.importorskip("docker")
    from pydantic_ai_backends import DockerSandbox

    sandbox = DockerSandbox()
    yield sandbox
    sandbox.stop()


class TestDockerSandboxInit:
    """Tests for DockerSandbox initialization parameters."""

    def test_init_default_values(self):
        """Test default initialization values."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        # Call __init__ manually to test parameter defaults
        sandbox.__init__()

        assert sandbox._image == "python:3.12-slim"
        assert sandbox._work_dir == "/workspace"
        assert sandbox._auto_remove is True
        assert sandbox._idle_timeout == 3600
        assert sandbox._volumes == {}
        assert sandbox._network_mode is None
        assert sandbox._runtime is None

    def test_init_with_volumes(self):
        """Test initialization with volumes parameter."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host/path": "/container/path"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=volumes)

        assert sandbox._volumes == volumes

    def test_init_with_empty_volumes(self):
        """Test initialization with empty volumes dict."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes={})

        assert sandbox._volumes == {}

    def test_init_with_none_volumes(self):
        """Test initialization with None volumes (default)."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=None)

        assert sandbox._volumes == {}

    def test_init_with_multiple_volumes(self):
        """Test initialization with multiple volume mappings."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {
            "/host/workspace": "/workspace",
            "/host/data": "/data",
            "/host/config": "/config",
        }
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(volumes=volumes)

        assert sandbox._volumes == volumes
        assert len(sandbox._volumes) == 3

    def test_init_with_all_parameters(self):
        """Test initialization with all parameters including volumes."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host/path": "/workspace"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(
            image="python:3.11",
            sandbox_id="test-sandbox",
            work_dir="/app",
            auto_remove=False,
            idle_timeout=7200,
            volumes=volumes,
            network_mode="none",
        )

        assert sandbox._image == "python:3.11"
        assert sandbox._id == "test-sandbox"
        assert sandbox._work_dir == "/app"
        assert sandbox._auto_remove is False
        assert sandbox._idle_timeout == 7200
        assert sandbox._volumes == volumes
        assert sandbox._network_mode == "none"

    def test_init_default_network_mode(self):
        """Test default network_mode is None."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__()

        assert sandbox._network_mode is None

    def test_init_with_network_mode(self):
        """Test initialization with network_mode parameter."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(network_mode="none")

        assert sandbox._network_mode == "none"

    def test_init_with_session_id_alias(self):
        """Test that session_id works as alias for sandbox_id."""
        from pydantic_ai_backends import DockerSandbox

        volumes = {"/host": "/container"}
        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(session_id="my-session", volumes=volumes)

        assert sandbox._id == "my-session"
        assert sandbox._volumes == volumes


class TestDockerTimeoutEscaping:
    """Tests for timeout command escaping (fixes command!r bug).

    These tests verify that commands with quotes, variables, and pipes work
    correctly when timeout is specified. Previously failed due to command!r bug.
    """

    @pytest.mark.docker
    def test_execute_timeout_with_quotes(self, docker_sandbox):
        """Test execute with timeout handles quoted strings correctly.

        Previously failed because command!r added extra quotes:
        command = "echo 'hello world'"
        command!r = "'echo \\'hello world\\''"  # BAD - extra quotes
        """
        # Command with double quotes
        result = docker_sandbox.execute('echo "hello world"', timeout=5)
        assert result.exit_code == 0
        assert "hello world" in result.output

        # Command with single quotes
        result = docker_sandbox.execute("echo 'goodbye world'", timeout=5)
        assert result.exit_code == 0
        assert "goodbye world" in result.output

    @pytest.mark.docker
    def test_execute_timeout_with_variables(self, docker_sandbox):
        """Test execute with timeout handles shell variables correctly.

        Previously failed because command!r escaped $ incorrectly:
        command = "echo $HOME"
        command!r = "'echo $HOME'"  # $ gets escaped/not expanded
        """
        # Shell variable expansion
        result = docker_sandbox.execute("echo $HOME", timeout=5)
        assert result.exit_code == 0
        # HOME should be expanded (not literal "$HOME")
        assert "$HOME" not in result.output or result.output.strip() != "$HOME"

        # Command substitution
        result = docker_sandbox.execute("echo $(pwd)", timeout=5)
        assert result.exit_code == 0
        assert result.output.strip()  # Should output the working directory

    @pytest.mark.docker
    def test_execute_timeout_with_pipes(self, docker_sandbox):
        """Test execute with timeout handles pipes and redirects correctly.

        Previously failed because command!r broke shell piping:
        command = "echo test | grep test"
        command!r = "'echo test | grep test'"  # Pipe becomes literal string
        """
        # Pipe command
        result = docker_sandbox.execute("echo 'test line' | grep test", timeout=5)
        assert result.exit_code == 0
        assert "test line" in result.output

        # Multiple pipes
        result = docker_sandbox.execute("echo 'hello world' | tr a-z A-Z | grep HELLO", timeout=5)
        assert result.exit_code == 0
        assert "HELLO WORLD" in result.output


class TestDockerSandboxEdit:
    """Tests for DockerSandbox.edit() method using Python string operations."""

    @pytest.mark.docker
    def test_edit_basic_single_occurrence(self, docker_sandbox):
        """Test basic edit with single occurrence."""
        # Write a simple file
        docker_sandbox.write("/workspace/test1.txt", "Hello, World!")

        # Edit single occurrence
        result = docker_sandbox.edit("/workspace/test1.txt", "World", "Universe")

        assert result.error is None
        assert result.occurrences == 1

        # Verify the change
        content = docker_sandbox.read("/workspace/test1.txt")
        assert "Universe" in content
        assert "World" not in content

    @pytest.mark.docker
    def test_edit_multiline_string(self, docker_sandbox):
        """Test editing multiline strings (main improvement over sed approach)."""
        # Write file with multiline content
        original = "def foo():\n    return 'old'\n\nprint('test')"
        docker_sandbox.write("/workspace/code.py", original)

        # Edit multiline string (this would fail with sed approach)
        old_function = "def foo():\n    return 'old'"
        new_function = "def foo():\n    return 'new'"

        result = docker_sandbox.edit("/workspace/code.py", old_function, new_function)

        assert result.error is None
        assert result.occurrences == 1

        # Verify the multiline replacement worked
        content = docker_sandbox.read("/workspace/code.py")
        assert "return 'new'" in content
        assert "return 'old'" not in content
        assert "print('test')" in content  # Rest of file unchanged

    @pytest.mark.docker
    def test_edit_multiple_occurrences_replace_all(self, docker_sandbox):
        """Test editing with multiple occurrences using replace_all."""
        # Write file with multiple occurrences
        docker_sandbox.write("/workspace/multi.txt", "foo bar foo baz foo")

        # Should fail without replace_all
        result = docker_sandbox.edit("/workspace/multi.txt", "foo", "qux")
        assert result.error is not None
        assert "3 times" in result.error

        # Should succeed with replace_all=True
        result = docker_sandbox.edit("/workspace/multi.txt", "foo", "qux", replace_all=True)
        assert result.error is None
        assert result.occurrences == 3

        # Verify all occurrences replaced
        content = docker_sandbox.read("/workspace/multi.txt")
        assert "qux" in content
        assert "foo" not in content
        assert content.count("qux") == 3


class TestDockerSandboxReadTextHandling:
    """Unit tests for the sandbox read helpers that don't require Docker."""

    def test_convert_bytes_to_text_uses_mimetype_detection(self, monkeypatch):
        """Ensure files detected as text via mimetypes are decoded as text."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)

        decoded_value = "decoded via mimetype"

        def fake_decode(self, file_bytes):
            return decoded_value

        monkeypatch.setitem(mimetypes.types_map, ".cfg", "text/x-config")
        monkeypatch.setattr(DockerSandbox, "_decode_text", fake_decode, raising=False)

        result = sandbox._convert_bytes_to_text("cfg", b"whatever")
        assert result == decoded_value

    def test_decode_unknown_text_binary_fallback(self, monkeypatch):
        """When no encoding decodes cleanly, binary marker should be returned."""
        import pydantic_ai_backends.backends.docker.sandbox as sandbox_mod
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)

        # Force chardet to detect utf-8 so binary bytes produce many
        # replacement characters and trigger the binary heuristic.
        fake_chardet = type(
            "FakeChardet",
            (),
            {"detect": staticmethod(lambda b: {"encoding": "utf-8", "confidence": 0.5})},
        )()
        monkeypatch.setattr(sandbox_mod, "_get_chardet", lambda: fake_chardet)

        raw_bytes = bytearray(range(129, 255)) * 4
        with pytest.raises(ValueError, match="Binary File"):
            sandbox._decode_unknown_text(raw_bytes)


class TestDockerSandboxGrepRaw:
    """Tests for BaseSandbox.grep_raw default path behaviour."""

    @pytest.mark.docker
    def test_grep_raw_finds_match_without_explicit_path(self, docker_sandbox):
        """grep_raw with no path searches the working directory, not /."""
        docker_sandbox.write("/workspace/grep_target.txt", "hello_unique_sentinel\n")
        result = docker_sandbox.grep_raw("hello_unique_sentinel", ignore_hidden=False)
        assert isinstance(result, list)
        assert any("grep_target.txt" in m["path"] for m in result)

    @pytest.mark.docker
    def test_grep_raw_no_path_is_fast(self, docker_sandbox):
        """grep_raw with no path completes quickly, proving it searches . not /.

        Searching / inside a Docker container takes minutes; searching the
        workspace directory takes milliseconds.
        """
        start = time.monotonic()
        result = docker_sandbox.grep_raw("this_string_will_never_exist_xyzzy_99999")
        elapsed = time.monotonic() - start
        assert result == []
        assert elapsed < 5, f"grep_raw took {elapsed:.1f}s — likely searched / instead of ."


class TestDockerSandboxResolvePath:
    """Tests for _resolve_path helper (no Docker required)."""

    def test_resolve_path_relative(self):
        """Relative paths are resolved against work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("file.txt") == "/workspace/file.txt"

    def test_resolve_path_relative_nested(self):
        """Nested relative paths are resolved against work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("subdir/file.txt") == "/workspace/subdir/file.txt"

    def test_resolve_path_absolute(self):
        """Absolute paths pass through unchanged."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/workspace")

        assert sandbox._resolve_path("/custom/dir/file.txt") == "/custom/dir/file.txt"

    def test_resolve_path_custom_work_dir(self):
        """Relative paths resolve against a custom work_dir."""
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox.__new__(DockerSandbox)
        sandbox.__init__(work_dir="/custom/workspace")

        assert sandbox._resolve_path("file.txt") == "/custom/workspace/file.txt"


class TestDockerSandboxFilePathResolution:
    """Tests for file operations with relative and absolute paths (requires Docker)."""

    @pytest.mark.docker
    def test_read_file_absolute_path(self, docker_sandbox):
        """Write to an absolute path and read it back with an absolute path."""
        docker_sandbox.write("/custom/dir/file.txt", "absolute content")
        content = docker_sandbox.read("/custom/dir/file.txt")
        assert "absolute content" in content

    @pytest.mark.docker
    def test_read_file_relative_path(self, docker_sandbox):
        """Write to work_dir and read with a relative path."""
        docker_sandbox.write("/workspace/rel_test.txt", "relative content")
        content = docker_sandbox.read("rel_test.txt")
        assert "relative content" in content

    @pytest.mark.docker
    def test_write_file_relative_path(self, docker_sandbox):
        """Write using a relative path and read back with absolute path."""
        docker_sandbox.write("rel_write.txt", "written relatively")
        content = docker_sandbox.read("/workspace/rel_write.txt")
        assert "written relatively" in content

    @pytest.mark.docker
    def test_edit_file_relative_path(self, docker_sandbox):
        """Edit a file using a relative path."""
        docker_sandbox.write("edit_rel.txt", "old value")
        result = docker_sandbox.edit("edit_rel.txt", "old value", "new value")
        assert result.error is None
        content = docker_sandbox.read("edit_rel.txt")
        assert "new value" in content

    @pytest.mark.docker
    def test_read_file_nonexistent_path(self, docker_sandbox):
        """Reading a non-existent file returns an error string, not a crash."""
        content = docker_sandbox.read("/workspace/does_not_exist.txt")
        assert "Error" in content
        assert "not found" in content

    @pytest.mark.docker
    def test_read_bytes_nonexistent_path(self, docker_sandbox):
        """_read_bytes on a non-existent path returns empty bytes."""
        result = docker_sandbox._read_bytes("/workspace/no_such_file.bin")
        assert result == b""

    @pytest.mark.docker
    def test_exists_returns_true_for_existing_file(self, docker_sandbox):
        """exists() returns True for a file written into the container."""
        docker_sandbox.write("/workspace/exists_check.txt", "hi")
        assert docker_sandbox.exists("/workspace/exists_check.txt") is True

    @pytest.mark.docker
    def test_exists_returns_false_for_missing_file(self, docker_sandbox):
        """exists() returns False for an unwritten path inside the container."""
        assert docker_sandbox.exists("/workspace/never_written.txt") is False

    @pytest.mark.docker
    def test_exists_returns_false_for_directory(self, docker_sandbox):
        """exists() returns False for directories — ``test -f`` fails on dirs."""
        # /workspace is the work_dir, guaranteed to be a directory.
        assert docker_sandbox.exists("/workspace") is False


class TestDockerSandboxNetworkMode:
    """Tests for DockerSandbox network_mode parameter."""

    @pytest.mark.docker
    def test_network_mode_none_disables_networking(self):
        """Test that network_mode='none' prevents network access."""
        pytest.importorskip("docker")
        from pydantic_ai_backends import DockerSandbox

        sandbox = DockerSandbox(network_mode="none")
        try:
            result = sandbox.execute(
                "python -c \"import urllib.request; urllib.request.urlopen('http://example.com')\"",
                timeout=10,
            )
            assert result.exit_code != 0
        finally:
            sandbox.stop()
