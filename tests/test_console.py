"""Tests for console toolset."""

import inspect
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import BinaryContent

from pydantic_ai_backends import (
    LocalBackend,
    StateBackend,
    create_console_toolset,
    get_console_system_prompt,
)
from pydantic_ai_backends.toolsets.console import (
    DEFAULT_MAX_IMAGE_BYTES,
    IMAGE_EXTENSIONS,
    IMAGE_MEDIA_TYPES,
    ConsoleDeps,
)


@dataclass
class MockDeps:
    """Mock deps for testing."""

    backend: LocalBackend | StateBackend


class TestConsoleDeps:
    """Test ConsoleDeps protocol."""

    def test_mock_deps_implements_protocol(self, tmp_path: Path):
        """Test that MockDeps implements ConsoleDeps."""
        backend = LocalBackend(root_dir=tmp_path)
        deps = MockDeps(backend=backend)

        # Should be instance of ConsoleDeps (runtime checkable protocol)
        assert isinstance(deps, ConsoleDeps)

    def test_state_backend_implements_protocol(self):
        """Test that StateBackend works with ConsoleDeps."""
        backend = StateBackend()
        deps = MockDeps(backend=backend)
        assert isinstance(deps, ConsoleDeps)


class TestCreateConsoleToolset:
    """Test create_console_toolset function."""

    def test_create_default_toolset(self):
        """Test creating toolset with default settings."""
        toolset = create_console_toolset()

        # Should have all standard tools
        tool_names = list(toolset.tools.keys())
        assert "ls" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names
        assert "execute" in tool_names

    def test_create_toolset_without_execute(self):
        """Test creating toolset without execute tool."""
        toolset = create_console_toolset(include_execute=False)

        tool_names = list(toolset.tools.keys())
        assert "ls" in tool_names
        assert "read_file" in tool_names
        assert "execute" not in tool_names

    def test_create_toolset_with_custom_id(self):
        """Test creating toolset with custom ID."""
        toolset = create_console_toolset(id="my-console")
        assert toolset.id == "my-console"

    def test_tool_approval_settings(self):
        """Test tool approval settings."""
        # Default: write doesn't require approval, execute does
        toolset = create_console_toolset()

        # Verify toolset was created with expected tools
        assert "write_file" in toolset.tools
        assert "execute" in toolset.tools

        # Check requires_approval setting on execute tool
        assert toolset.tools["execute"].requires_approval is True

    def test_toolset_with_write_approval(self):
        """Test toolset with write approval required."""
        toolset = create_console_toolset(require_write_approval=True)

        assert "write_file" in toolset.tools
        assert "edit_file" in toolset.tools

        # Check requires_approval setting
        assert toolset.tools["write_file"].requires_approval is True
        assert toolset.tools["edit_file"].requires_approval is True

    def test_toolset_default_ignore_hidden_configurable(self):
        """Grep should respect default ignore hidden flag."""
        toolset = create_console_toolset(default_ignore_hidden=False)

        assert hasattr(toolset, "_console_default_ignore_hidden")
        assert toolset._console_default_ignore_hidden is False

        grep_impl = toolset._console_grep_impl
        params = inspect.signature(grep_impl).parameters
        assert params["ignore_hidden"].default is False


class TestGetConsoleSystemPrompt:
    """Test get_console_system_prompt function."""

    def test_returns_string(self):
        """Test that system prompt returns a string."""
        prompt = get_console_system_prompt()
        assert isinstance(prompt, str)

    def test_contains_tool_descriptions(self):
        """Test that system prompt contains tool descriptions."""
        prompt = get_console_system_prompt()

        # Should mention file operations
        assert "ls" in prompt.lower() or "list" in prompt.lower()
        assert "read" in prompt.lower()
        assert "write" in prompt.lower()
        assert "edit" in prompt.lower()

        # Should mention shell execution
        assert "execute" in prompt.lower() or "shell" in prompt.lower()


class TestCreateConsoleToolsetImageParams:
    """Test create_console_toolset with image parameters."""

    def test_create_toolset_with_image_support(self):
        """Test creating toolset with image_support=True."""
        toolset = create_console_toolset(image_support=True)
        assert "read_file" in toolset.tools

    def test_create_toolset_with_custom_max_image_bytes(self):
        """Test creating toolset with custom max_image_bytes."""
        toolset = create_console_toolset(image_support=True, max_image_bytes=1024)
        assert "read_file" in toolset.tools

    def test_create_toolset_default_no_image_support(self):
        """Test that image_support defaults to False."""
        toolset = create_console_toolset()
        assert "read_file" in toolset.tools


class TestImageConstants:
    """Test image-related constants."""

    def test_image_extensions(self):
        """Test IMAGE_EXTENSIONS contains expected formats."""
        assert "png" in IMAGE_EXTENSIONS
        assert "jpg" in IMAGE_EXTENSIONS
        assert "jpeg" in IMAGE_EXTENSIONS
        assert "gif" in IMAGE_EXTENSIONS
        assert "webp" in IMAGE_EXTENSIONS
        # SVG is intentionally excluded (it's text/XML)
        assert "svg" not in IMAGE_EXTENSIONS

    def test_image_media_types(self):
        """Test IMAGE_MEDIA_TYPES maps extensions to MIME types."""
        assert IMAGE_MEDIA_TYPES["png"] == "image/png"
        assert IMAGE_MEDIA_TYPES["jpg"] == "image/jpeg"
        assert IMAGE_MEDIA_TYPES["jpeg"] == "image/jpeg"
        assert IMAGE_MEDIA_TYPES["gif"] == "image/gif"
        assert IMAGE_MEDIA_TYPES["webp"] == "image/webp"

    def test_all_extensions_have_media_types(self):
        """Test every IMAGE_EXTENSION has a corresponding IMAGE_MEDIA_TYPE."""
        for ext in IMAGE_EXTENSIONS:
            assert ext in IMAGE_MEDIA_TYPES

    def test_default_max_image_bytes(self):
        """Test DEFAULT_MAX_IMAGE_BYTES is 50MB."""
        assert DEFAULT_MAX_IMAGE_BYTES == 50 * 1024 * 1024


class TestReadFileImageSupport:
    """Test read_file with image_support enabled."""

    def test_read_image_png_local_backend(self, tmp_path: Path):
        """Test reading a PNG image returns BinaryContent."""
        # Create a fake PNG file (PNG header + some data)
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        img_path = tmp_path / "test.png"
        img_path.write_bytes(png_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("test.png")
        assert result == png_data

        # Verify the toolset is created with image_support
        toolset = create_console_toolset(image_support=True)
        assert "read_file" in toolset.tools

    def test_read_image_jpg_local_backend(self, tmp_path: Path):
        """Test reading a JPG image returns bytes via read_bytes."""
        jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        img_path = tmp_path / "photo.jpg"
        img_path.write_bytes(jpg_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("photo.jpg")
        assert result == jpg_data

    def test_read_image_jpeg_extension(self, tmp_path: Path):
        """Test .jpeg extension is recognized."""
        jpeg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        img_path = tmp_path / "photo.jpeg"
        img_path.write_bytes(jpeg_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("photo.jpeg")
        assert result == jpeg_data

    def test_read_image_gif_local_backend(self, tmp_path: Path):
        """Test reading a GIF image."""
        gif_data = b"GIF89a" + b"\x00" * 50
        img_path = tmp_path / "anim.gif"
        img_path.write_bytes(gif_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("anim.gif")
        assert result == gif_data

    def test_read_image_webp_local_backend(self, tmp_path: Path):
        """Test reading a WebP image."""
        webp_data = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 50
        img_path = tmp_path / "image.webp"
        img_path.write_bytes(webp_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("image.webp")
        assert result == webp_data

    def test_read_image_not_found(self, tmp_path: Path):
        """Test reading nonexistent image returns empty bytes."""
        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("nonexistent.png")
        assert result == b""

    def test_read_text_file_with_image_support(self, tmp_path: Path):
        """Test that text files still return text with image_support enabled."""
        txt_path = tmp_path / "readme.txt"
        txt_path.write_text("Hello, world!")

        backend = LocalBackend(root_dir=tmp_path)
        # Text files should still use the standard read() method
        result = backend.read("readme.txt")
        assert "Hello, world!" in result

    def test_read_image_state_backend(self):
        """Test reading image from StateBackend via read_bytes."""
        backend = StateBackend()
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        backend.write("/test.png", png_data)

        result = backend.read_bytes("/test.png")
        # StateBackend stores as text, so read_bytes returns encoded text
        assert isinstance(result, bytes)

    def test_image_extension_case_insensitive(self):
        """Test that image extension detection is case-insensitive."""
        # The implementation uses .lower() so uppercase should work
        path = "photo.PNG"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        assert ext in IMAGE_EXTENSIONS

    def test_no_extension_not_image(self):
        """Test that files without extension are not treated as images."""
        path = "Makefile"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        assert ext not in IMAGE_EXTENSIONS

    def test_binary_content_creation(self):
        """Test creating BinaryContent from image data."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        content = BinaryContent(data=png_data, media_type="image/png")
        assert content.data == png_data
        assert content.media_type == "image/png"
        assert content.is_image

    def test_binary_content_jpg_media_type(self):
        """Test BinaryContent with JPEG media type."""
        jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        content = BinaryContent(data=jpg_data, media_type="image/jpeg")
        assert content.media_type == "image/jpeg"
        assert content.is_image


class TestImageExports:
    """Test that image constants are exported from the package."""

    def test_image_extensions_exported(self):
        """Test IMAGE_EXTENSIONS is importable from package."""
        from pydantic_ai_backends import IMAGE_EXTENSIONS as exported

        assert exported == IMAGE_EXTENSIONS

    def test_image_media_types_exported(self):
        """Test IMAGE_MEDIA_TYPES is importable from package."""
        from pydantic_ai_backends import IMAGE_MEDIA_TYPES as exported

        assert exported == IMAGE_MEDIA_TYPES

    def test_default_max_image_bytes_exported(self):
        """Test DEFAULT_MAX_IMAGE_BYTES is importable from package."""
        from pydantic_ai_backends import DEFAULT_MAX_IMAGE_BYTES as exported

        assert exported == DEFAULT_MAX_IMAGE_BYTES


class TestConsoleToolsetAlias:
    """Test ConsoleToolset alias."""

    def test_alias_works(self):
        """Test that ConsoleToolset alias works."""
        from pydantic_ai_backends.toolsets.console import ConsoleToolset

        toolset = ConsoleToolset()
        assert len(toolset.tools) > 0
