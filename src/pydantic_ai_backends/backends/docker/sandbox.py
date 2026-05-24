"""Sandbox backends for isolated command execution."""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import shlex
import tarfile
import time
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends.backends.base import (
    CODE_EXT,
    TEXT_EXT,
    BaseSandbox,
    _get_chardet,
    _get_pypdf,
)
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    RuntimeConfig,
    WriteResult,
)

if TYPE_CHECKING:
    pass


class DockerSandbox(BaseSandbox):  # pragma: no cover
    """Docker-based sandbox for isolated command execution.

    Creates a Docker container for running commands in an isolated environment.
    Requires the docker Python package to be installed.

    Supports RuntimeConfig for pre-configured environments with packages pre-installed.

    Example:
        ```python
        from pydantic_ai_backends import DockerSandbox, RuntimeConfig

        # Use a simple image
        sandbox = DockerSandbox(image="python:3.12-slim")

        # Or use a custom runtime with packages
        custom_runtime = RuntimeConfig(
            name="ml-env",
            base_image="python:3.12-slim",
            packages=["torch", "transformers"],
        )
        sandbox = DockerSandbox(runtime=custom_runtime)
        ```
    """

    def __init__(
        self,
        image: str = "python:3.12-slim",
        sandbox_id: str | None = None,
        work_dir: str = "/workspace",
        auto_remove: bool = True,
        runtime: RuntimeConfig | str | None = None,
        session_id: str | None = None,
        idle_timeout: int = 3600,
        volumes: dict[str, str] | None = None,
        network_mode: str | None = None,
        container_name: str | None = None,
    ):
        """Initialize Docker sandbox.

        Args:
            image: Docker image to use (ignored if runtime is provided).
            sandbox_id: Unique identifier for this sandbox.
            work_dir: Working directory inside container (ignored if runtime is provided).
            auto_remove: Remove container when stopped. Forced to ``False``
                when ``container_name`` is set (reusable containers).
            runtime: RuntimeConfig or name of built-in runtime.
            session_id: Alias for sandbox_id (for session management).
            idle_timeout: Timeout in seconds for idle cleanup (default: 1 hour).
            volumes: Host-to-container volume mappings for persistent storage.
                     Format: {"/host/path": "/container/path"}
            network_mode: Docker network mode ("bridge", "none", "host",
                         "container:<name|id>"). None uses Docker default.
            container_name: Stable Docker container name for reuse across
                restarts. When set, ``_ensure_container()`` looks for an existing
                container with this name and reattaches (or starts it if stopped)
                instead of creating a new one. Implies ``auto_remove=False``.
        """
        # session_id is an alias for sandbox_id
        effective_id = session_id or sandbox_id
        super().__init__(effective_id)

        self._container_name = container_name
        # Named containers must not be auto-removed (they're meant to be reused)
        self._auto_remove = False if container_name else auto_remove
        self._container = None
        self._idle_timeout = idle_timeout
        self._last_activity = time.time()
        self._volumes = volumes or {}
        self._network_mode = network_mode

        # Handle runtime configuration
        if runtime is not None:
            if isinstance(runtime, str):
                from pydantic_ai_backends.backends.docker.runtimes import get_runtime

                runtime = get_runtime(runtime)
            self._runtime: RuntimeConfig | None = runtime
            self._work_dir = runtime.work_dir
            self._image = image  # Will be overridden by _ensure_runtime_image()
        else:
            self._runtime = None
            self._work_dir = work_dir
            self._image = image

    @property
    def runtime(self) -> RuntimeConfig | None:
        """The runtime configuration for this sandbox."""
        return self._runtime

    @property
    def session_id(self) -> str:
        """Alias for sandbox id, used for session management."""
        return self._id

    def _resolve_path(self, path: str) -> str:
        """Resolve relative paths against the container's working directory."""
        if not PurePosixPath(path).is_absolute():
            return str(PurePosixPath(self._work_dir) / path)
        return path

    def _ensure_container(self) -> None:
        """Ensure Docker container is running.

        When ``container_name`` is set, looks for an existing container with
        that name and reattaches.  Stopped containers are restarted so that
        installed packages, caches, and other filesystem state are preserved
        across CLI sessions.
        """
        if self._container is not None:
            return

        try:
            import docker
            import docker.errors
        except ImportError as e:
            raise ImportError(
                "Docker package not installed. "
                "Install with: pip install pydantic-ai-backend[docker]"
            ) from e

        client = docker.from_env()

        # Try to reattach to an existing named container
        if self._container_name:
            try:
                existing = client.containers.get(self._container_name)
                status = existing.status
                if status == "running":
                    self._container = existing
                    return
                if status in ("created", "exited", "paused"):
                    existing.start()
                    self._container = existing
                    return
                # Other statuses (dead, removing) — fall through to create
            except docker.errors.NotFound:
                pass  # Will create a new container below

        # Get the appropriate image (build if needed for runtime)
        image = self._ensure_runtime_image(client)

        # Prepare environment variables from runtime
        env_vars = {}
        if self._runtime and self._runtime.env_vars:
            env_vars = self._runtime.env_vars

        # Convert simple volume format to Docker SDK format
        # {"/host": "/container"} -> {"/host": {"bind": "/container", "mode": "rw"}}
        docker_volumes: dict[str, dict[str, str]] = {}
        for host_path, container_path in self._volumes.items():
            docker_volumes[host_path] = {"bind": container_path, "mode": "rw"}

        run_kwargs: dict[str, Any] = dict(
            command="sleep infinity",
            detach=True,
            working_dir=self._work_dir,
            auto_remove=self._auto_remove,
            environment=env_vars,
            volumes=docker_volumes if docker_volumes else None,
        )
        if self._container_name is not None:
            run_kwargs["name"] = self._container_name
        if self._network_mode is not None:
            run_kwargs["network_mode"] = self._network_mode

        self._container = client.containers.run(image, **run_kwargs)

    def _ensure_runtime_image(self, client: object) -> str:
        """Ensure runtime image exists and return its name.

        Args:
            client: Docker client instance.

        Returns:
            Docker image name/tag to use.
        """
        if self._runtime is None:
            return self._image

        # If ready-to-use image is specified
        if self._runtime.image:
            return self._runtime.image

        # If base_image + packages - need to build
        if self._runtime.base_image:
            return self._build_runtime_image(client)

        # Fallback to default image
        return self._image

    def _build_runtime_image(self, client: object) -> str:
        """Build a custom image with packages installed.

        Args:
            client: Docker client instance.

        Returns:
            Docker image tag for the built image.
        """
        import docker.errors

        runtime = self._runtime
        assert runtime is not None
        assert runtime.base_image is not None

        # Generate unique tag based on config
        config_hash = hashlib.md5(runtime.model_dump_json().encode()).hexdigest()[:12]
        image_tag = f"pydantic-ai-backend-runtime:{runtime.name}-{config_hash}"

        # Check if image exists (cache)
        if runtime.cache_image:
            try:
                client.images.get(image_tag)  # type: ignore[attr-defined]
                return image_tag
            except docker.errors.ImageNotFound:
                pass

        # Build Dockerfile
        dockerfile = self._generate_dockerfile(runtime)

        # Build image
        client.images.build(  # type: ignore[attr-defined]
            fileobj=io.BytesIO(dockerfile.encode()),
            tag=image_tag,
            rm=True,
        )

        return image_tag

    def _generate_dockerfile(self, runtime: RuntimeConfig) -> str:
        """Generate Dockerfile content for runtime.

        Args:
            runtime: Runtime configuration.

        Returns:
            Dockerfile content as string.
        """
        assert runtime.base_image is not None
        lines = [f"FROM {runtime.base_image}"]

        # Setup commands
        for cmd in runtime.setup_commands:
            lines.append(f"RUN {cmd}")

        # Install packages
        if runtime.packages:
            packages_str = " ".join(runtime.packages)
            if runtime.package_manager == "pip":
                lines.append(f"RUN pip install --no-cache-dir {packages_str}")
            elif runtime.package_manager == "npm":
                lines.append(f"RUN npm install -g {packages_str}")
            elif runtime.package_manager == "apt":
                lines.append(f"RUN apt-get update && apt-get install -y {packages_str}")
            elif runtime.package_manager == "cargo":
                lines.append(f"RUN cargo install {packages_str}")

        # Environment variables
        for key, value in runtime.env_vars.items():
            lines.append(f"ENV {key}={value}")

        # Work directory
        lines.append(f"WORKDIR {runtime.work_dir}")

        return "\n".join(lines)

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Execute command in Docker container."""
        self._ensure_container()
        self._last_activity = time.time()  # Update activity timestamp
        assert self._container is not None  # Ensured by _ensure_container()

        try:
            # Note: Docker SDK exec_run doesn't support timeout parameter directly.
            # For timeouts, we wrap the command with 'timeout' utility.
            if timeout:
                exec_cmd = ["timeout", str(timeout), "sh", "-c", command]
            else:
                exec_cmd = ["sh", "-c", command]

            exit_code, output = self._container.exec_run(
                exec_cmd,
                workdir=self._work_dir,
            )

            if not isinstance(output, bytes):
                output = b"".join(output)
            output_str = output.decode("utf-8", errors="replace")

            # Truncate if too long
            max_output = 100000
            truncated = len(output_str) > max_output
            if truncated:
                output_str = output_str[:max_output]

            return ExecuteResponse(
                output=output_str,
                exit_code=exit_code,
                truncated=truncated,
            )
        except Exception as e:
            return ExecuteResponse(
                output=f"Error: {e}",
                exit_code=1,
                truncated=False,
            )

    def read_bytes(self, path: str) -> bytes:
        """Read raw bytes from file in container.

        Args:
            path: Path to the file in the container.

        Returns:
            File content as bytes, or empty bytes if the file does not
            exist or cannot be read.
        """
        path = self._resolve_path(path)
        self._ensure_container()
        assert self._container is not None

        try:
            # Use Docker get_archive to read file
            stream, stat = self._container.get_archive(path)
            raw_tar_bytes = b"".join(stream)
        except Exception:
            return b""

        # Extract file from tar archive
        try:
            with (
                io.BytesIO(raw_tar_bytes) as tar_buffer,
                tarfile.open(fileobj=tar_buffer, mode="r") as tar,
            ):
                member = next((m for m in tar.getmembers() if m.isfile()), None)

                if not member:
                    return b""

                f = tar.extractfile(member)
                if f is None:
                    return b""

                return f.read()
        except Exception:
            return b""

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """
        Read file from container using Docker get_archive API.

        Args:
            path: Path to the file in the container.
            offset: Start line index (for pagination).
            limit: Maximum number of lines to return.
        """
        original_path = path
        path = self._resolve_path(path)
        try:
            # Read raw bytes from file
            file_bytes = self.read_bytes(path)
            if not file_bytes:
                return f"Error: File '{original_path}' not found"

            # Convert bytes to string
            file_ext = Path(path).suffix.lower().lstrip(".")
            try:
                full_text = self._convert_bytes_to_text(file_ext, file_bytes)
            except ValueError as e:
                return f"[Error: {e}]"

            # Split into lines
            lines = full_text.splitlines()
            total_lines = len(lines)

            if offset >= total_lines:
                return "[End of file]"

            end_index = offset + limit
            chunk_lines = lines[offset:end_index]
            chunk = "\n".join(chunk_lines)

            if end_index < total_lines:
                remaining = total_lines - end_index
                footer = f"\n\n[... {remaining} more lines. Use offset={end_index} to read more.]"
                return chunk + footer

            return chunk

        except Exception as e:
            return f"[Error reading file: {e}]"

    def _convert_bytes_to_text(self, file_ext: str, file_bytes: bytes) -> str:
        # Plain text files with encoding detection
        if file_ext in (TEXT_EXT | CODE_EXT):
            return self._decode_text(file_bytes)

        mime_type = mimetypes.types_map.get(f".{file_ext}")
        if mime_type and (mime_type.startswith("text") or "json" in mime_type):
            return self._decode_text(file_bytes)

        # PDF files
        elif file_ext == "pdf":
            return self._extract_pdf_text(file_bytes)

        return self._decode_unknown_text(file_bytes)

    def _decode_text(self, file_bytes: bytes) -> str:
        chardet = _get_chardet()

        # Use chardet to detect encoding with confidence
        detection = chardet.detect(file_bytes)
        detected_encoding = detection.get("encoding")
        confidence = detection.get("confidence", 0)

        # If high confidence detection, use it
        if detected_encoding and confidence > 0.7:
            try:
                return file_bytes.decode(detected_encoding)
            except (UnicodeDecodeError, AttributeError, LookupError):
                pass  # Fall through to manual attempts

        # Fallback to common encodings if detection failed or low confidence
        encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1"]

        # Add detected encoding to the front if not already there
        if detected_encoding and detected_encoding not in encodings:
            encodings.insert(0, detected_encoding)

        for encoding in encodings:
            try:
                return file_bytes.decode(encoding)
            except (UnicodeDecodeError, AttributeError, LookupError):
                continue

        # Last resort: decode with errors='replace' to avoid complete failure
        return file_bytes.decode("utf-8", errors="replace")

    def _decode_unknown_text(self, file_bytes: bytes) -> str:
        chardet = _get_chardet()
        # Use chardet to detect encoding with confidence
        detected_encoding = chardet.detect(file_bytes).get("encoding")
        # Fallback to common encodings if detection failed or low confidence
        encodings = {detected_encoding, "utf-8"} if detected_encoding else ["utf-8"]
        for encoding in encodings:
            text = file_bytes.decode(encoding, errors="replace")
            if text.count("\ufffd") < max(len(text) // 100, 2):
                return text
        raise ValueError("[Binary File]")

    def _extract_pdf_text(self, file_bytes: bytes) -> str:
        pypdf = _get_pypdf()

        try:
            pdf_file = BytesIO(file_bytes)
            pdf_reader = pypdf.PdfReader(pdf_file)

            if len(pdf_reader.pages) == 0:
                raise ValueError("PDF contains no pages")

            # Extract metadata for context
            metadata = pdf_reader.metadata
            text_parts = []

            if metadata:
                if metadata.get("/Title"):
                    text_parts.append(f"Title: {metadata['/Title']}\n")
                if metadata.get("/Author"):
                    text_parts.append(f"Author: {metadata['/Author']}\n")
                if metadata.get("/Subject"):
                    text_parts.append(f"Subject: {metadata['/Subject']}\n")
                text_parts.append("\n")

            # Extract text from each page with clear separators
            for page_num, page in enumerate(pdf_reader.pages, 1):
                page_text = page.extract_text()

                if page_text and page_text.strip():
                    # Clean up common PDF artifacts
                    page_text = self._clean_pdf_text(page_text)
                    text_parts.append(f"--- Page {page_num} ---\n")
                    text_parts.append(page_text)
                    text_parts.append("\n\n")

            full_text = "".join(text_parts).strip()

            if not full_text:
                raise ValueError("No extractable text found in PDF")

            return full_text

        except Exception as e:
            raise ValueError(f"Failed to parse PDF: {str(e)}") from e

    def _clean_pdf_text(self, text: str) -> str:
        """
        Clean common PDF text extraction artifacts for better LLM processing.

        Args:
            text: Raw extracted text

        Returns:
            Cleaned text
        """

        # Remove excessive whitespace while preserving paragraph breaks
        text = re.sub(r" +", " ", text)  # Multiple spaces to single space
        text = re.sub(r"\n ", "\n", text)  # Remove leading spaces on lines
        text = re.sub(r" \n", "\n", text)  # Remove trailing spaces on lines
        text = re.sub(r"\n{3,}", "\n\n", text)  # Max 2 consecutive newlines

        # Fix common hyphenation issues at line breaks
        text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)

        # Remove form feed characters
        text = text.replace("\f", "\n")

        return text.strip()

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit file using Python string operations instead of sed.

        This method reads the entire file, performs string replacement in Python,
        and writes it back. This approach handles multiline strings naturally
        without shell escaping issues.

        Args:
            path: Path to the file in the container.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences. If False, only replace first.

        Returns:
            EditResult with path and occurrence count on success, or error message.
        """
        original_path = path
        path = self._resolve_path(path)
        try:
            # Read the file content
            file_bytes = self.read_bytes(path)

            if not file_bytes:
                return EditResult(error=f"File '{original_path}' not found")

            # Decode to string using the same logic as read()
            file_ext = Path(path).suffix.lower().lstrip(".")
            try:
                content = self._convert_bytes_to_text(file_ext, file_bytes)
            except ValueError as e:
                return EditResult(error=str(e))

            # Count occurrences
            occurrences = content.count(old_string)

            if occurrences == 0:
                return EditResult(error="String not found in file")

            if occurrences > 1 and not replace_all:
                return EditResult(
                    error=f"String found {occurrences} times. "
                    "Use replace_all=True to replace all, or provide more context."
                )

            new_content = content.replace(old_string, new_string)

            # Write back the modified content
            write_result = self.write(path, new_content)

            if write_result.error:
                return EditResult(error=write_result.error)

            return EditResult(path=path, occurrences=occurrences)

        except Exception as e:
            return EditResult(error=f"Failed to edit file: {e}")

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write file to container using Docker put_archive API.

        This method uses Docker's put_archive() instead of heredoc to handle
        large files and special characters reliably.

        Args:
            path: Path where the file should be written (absolute or relative to work_dir).
            content: File content as string or bytes.

        Returns:
            WriteResult with path on success, or error message on failure.
        """
        path = self._resolve_path(path)
        self._ensure_container()
        assert self._container is not None

        try:
            # Parse path into directory and filename
            posix_path = PurePosixPath(path)
            parent_dir = str(posix_path.parent)
            filename = posix_path.name

            # Ensure parent directory exists
            safe_parent_dir = shlex.quote(parent_dir)
            mkdir_result = self.execute(f"mkdir -p {safe_parent_dir}")
            if mkdir_result.exit_code != 0:
                return WriteResult(error=f"Failed to create directory: {mkdir_result.output}")

            # Create tar archive in memory
            content = content if isinstance(content, bytes) else content.encode()
            tar_buffer = io.BytesIO()

            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                # Create TarInfo for the file
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(content)
                tarinfo.mtime = int(time.time())
                tarinfo.mode = 0o644

                # Add file to archive
                tar.addfile(tarinfo, io.BytesIO(content))

            # Reset buffer position
            tar_buffer.seek(0)

            # Upload to container
            self._container.put_archive(parent_dir, tar_buffer)

            return WriteResult(path=path)

        except Exception as e:
            return WriteResult(error=f"Failed to write file: {e}")

    def start(self) -> None:
        """Explicitly start the container.

        This is useful for pre-warming containers before use.
        The container is normally started lazily on first operation.
        """
        self._ensure_container()

    def is_alive(self) -> bool:
        """Check if container is running.

        Returns:
            True if container is running, False otherwise.
        """
        if self._container is None:
            return False
        try:
            self._container.reload()
            return self._container.status == "running"
        except Exception:
            return False

    def stop(self) -> None:
        """Stop and remove the container."""
        import contextlib

        container = getattr(self, "_container", None)
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop()
            self._container = None

    def __del__(self) -> None:
        """Cleanup container on deletion."""
        if hasattr(self, "_container"):
            self.stop()
