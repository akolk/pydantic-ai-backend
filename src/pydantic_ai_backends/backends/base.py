"""Base sandbox class for isolated command execution."""

from __future__ import annotations

import shlex
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    FileInfo,
    GrepMatch,
    WriteResult,
)

if TYPE_CHECKING:
    pass

CODE_EXT: frozenset[str] = frozenset(
    {
        "py",
        "js",
        "java",
        "cpp",
        "c",
        "h",
        "cs",
        "rb",
        "go",
        "rs",
        "php",
        "html",
        "css",
        "sh",
        "sql",
        "ts",
        "jsx",
        "tsx",
    }
)
TEXT_EXT: frozenset[str] = frozenset(
    {"txt", "log", "md", "json", "xml", "csv", "yaml", "yml", "toml"}
)


def _get_chardet() -> Any:  # pragma: no cover
    """Lazy import for chardet."""
    try:
        import chardet

        return chardet
    except ImportError as e:
        raise ImportError(
            "chardet package required for encoding detection. "
            "Install with: pip install pydantic-ai-backend[docker]"
        ) from e


def _get_pypdf() -> Any:  # pragma: no cover
    """Lazy import for pypdf."""
    try:
        import pypdf

        return pypdf
    except ImportError as e:
        raise ImportError(
            "pypdf package required for PDF reading. "
            "Install with: pip install pydantic-ai-backend[docker]"
        ) from e


class BaseSandbox(ABC):
    """Abstract base class for sandbox backends.

    Sandboxes provide isolated environments for executing commands and
    managing files. Subclasses must implement the execute() method.
    """

    def __init__(self, sandbox_id: str | None = None):
        """Initialize the sandbox.

        Args:
            sandbox_id: Unique identifier for this sandbox. Generated if not provided.
        """
        self._id = sandbox_id or str(uuid.uuid4())  # pragma: no cover
        self._last_activity = time.time()  # pragma: no cover

    @property
    def id(self) -> str:
        """Unique identifier for this sandbox."""
        return self._id  # pragma: no cover

    def start(self) -> None:  # pragma: no cover  # noqa: B027
        """Start the sandbox.

        Override for eager initialization. The default is a no-op
        (sandboxes start lazily on first operation).
        """

    def is_alive(self) -> bool:  # pragma: no cover
        """Check if the sandbox is running.

        Returns:
            True if the sandbox is running and responsive, False otherwise.
        """
        return False

    def stop(self) -> None:  # pragma: no cover  # noqa: B027
        """Stop and clean up the sandbox."""

    @abstractmethod
    def execute(
        self, command: str, timeout: int | None = None
    ) -> ExecuteResponse:  # pragma: no cover
        """Execute a command in the sandbox.

        Args:
            command: Command to execute.
            timeout: Maximum execution time in seconds.

        Returns:
            ExecuteResponse with output, exit code, and truncation status.
        """
        ...

    @abstractmethod
    def edit(  # pragma: no cover
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by replacing strings.

        Args:
            path: File path to edit.
            old_string: String to find and replace.
            new_string: Replacement string.
            replace_all: If True, replace all occurrences. Otherwise, replace only first.

        Returns:
            EditResult with path, error, or occurrence count.
        """
        ...

    def exists(self, path: str) -> bool:  # pragma: no cover
        """Check existence via ``test -f`` in the sandbox shell."""
        result = self.execute(f"test -f {shlex.quote(path)}", timeout=5)
        return result.exit_code == 0

    def ls_info(self, path: str) -> list[FileInfo]:  # pragma: no cover
        """List files using ls command."""
        path = shlex.quote(path)
        result = self.execute(f"ls -la {path}")
        if result.exit_code != 0:
            return []

        entries: list[FileInfo] = []
        for line in result.output.strip().split("\n")[1:]:  # Skip total line
            if not line.strip():
                continue

            parts = line.split()
            if len(parts) < 9:
                continue

            perms = parts[0]
            size = int(parts[4]) if parts[4].isdigit() else None
            name = " ".join(parts[8:])

            if name in (".", ".."):
                continue

            full_path = f"{path.rstrip('/')}/{name}"
            entries.append(
                FileInfo(
                    name=name,
                    path=full_path,
                    is_dir=perms.startswith("d"),
                    size=size,
                )
            )

        return sorted(entries, key=lambda x: (not x["is_dir"], x["name"]))

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover
        """Read raw bytes from file using cat command."""
        path = shlex.quote(path)
        result = self.execute(f"cat {path}")

        if result.exit_code != 0:
            return f"[Error: {result.output}]".encode()

        return result.output.encode("utf-8", errors="replace")

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:  # pragma: no cover
        """Read file using cat command with line numbers."""
        # Use sed to handle offset and limit
        start = offset + 1  # sed is 1-indexed
        end = offset + limit

        path = shlex.quote(path)
        result = self.execute(f"sed -n '{start},{end}p' {path} | cat -n")

        if result.exit_code != 0:
            return f"Error: {result.output}"

        if result.truncated:
            return result.output + "\n\n... (output truncated)"

        return result.output

    def write(self, path: str, content: str) -> WriteResult:  # pragma: no cover
        """Write file using cat with heredoc."""
        # Escape special characters for heredoc
        escaped = content.replace("\\", "\\\\").replace("$", "\\$").replace("`", "\\`")

        # Use a unique delimiter
        delimiter = f"EOF_{uuid.uuid4().hex[:8]}"

        quoted_path = shlex.quote(path)
        command = (
            f"mkdir -p $(dirname {quoted_path}) && cat > {quoted_path} << '{delimiter}'\n"
            f"{escaped}\n"
            f"{delimiter}"
        )
        result = self.execute(command)

        if result.exit_code != 0:
            return WriteResult(error=result.output)

        return WriteResult(path=path)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:  # pragma: no cover
        """Find files using find command."""
        # Convert glob to find pattern
        path = shlex.quote(path)
        result = self.execute(f"find '{path}' -path '{pattern}' -type f 2>/dev/null")

        if result.exit_code != 0:
            return []

        entries: list[FileInfo] = []
        for full_file_path in result.output.splitlines():
            file_path = PurePosixPath(full_file_path)
            name = file_path.name
            entries.append(
                FileInfo(
                    name=name,
                    path=str(file_path),
                    is_dir=False,
                    size=None,
                )
            )

        return sorted(entries, key=lambda x: x["path"])

    def grep_raw(  # pragma: no cover
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Search using grep command."""
        search_path = path or "."

        search_path = shlex.quote(search_path)

        options = ["-rn"]
        if ignore_hidden:
            options.extend(["--exclude='.*'", "--exclude-dir='.*'"])
        if glob:
            options.append(f"--include='{glob}'")

        options_str = " ".join(options)
        cmd = f"grep {options_str} '{pattern}' {search_path}"

        result = self.execute(cmd)

        if result.exit_code == 1:  # No matches
            return []
        if result.exit_code != 0:
            return f"Error: {result.output}"

        matches: list[GrepMatch] = []
        for line in result.output.strip().split("\n"):
            if not line:
                continue

            # Parse grep output: file:line:content
            parts = line.split(":", 2)
            if len(parts) >= 3:
                try:
                    matches.append(
                        GrepMatch(
                            path=parts[0],
                            line_number=int(parts[1]),
                            line=parts[2],
                        )
                    )
                except ValueError:
                    continue

        return matches
