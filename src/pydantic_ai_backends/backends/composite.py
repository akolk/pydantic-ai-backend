"""Composite backend that routes operations by path prefix."""

from __future__ import annotations

from pydantic_ai_backends.protocol import BackendProtocol
from pydantic_ai_backends.types import EditResult, FileInfo, GrepMatch, WriteResult


class CompositeBackend:
    """Backend that routes operations to different backends by path prefix.

    Allows combining multiple backends (e.g., memory for temp files,
    filesystem for persistent storage) under a unified interface.

    Example:
        ```python
        from pydantic_ai_backends import CompositeBackend, StateBackend, FilesystemBackend

        backend = CompositeBackend(
            default=StateBackend(),  # Default for unmatched paths
            routes={
                "/project/": FilesystemBackend("/my/project"),
                "/workspace/": FilesystemBackend("/tmp/workspace"),
            },
        )

        # Routes to FilesystemBackend
        backend.write("/project/app.py", "...")

        # Routes to StateBackend (default)
        backend.write("/temp/scratch.txt", "...")
        ```
    """

    def __init__(
        self,
        default: BackendProtocol,
        routes: dict[str, BackendProtocol] | None = None,
    ):
        """Initialize composite backend.

        Args:
            default: Default backend for paths that don't match any route.
            routes: Dictionary mapping path prefixes to backends.
                    e.g., {"/memories/": store_backend, "/temp/": state_backend}
        """
        self._default = default
        self._routes = routes or {}

        # Sort routes by length (longest first) for correct matching
        self._sorted_prefixes = sorted(self._routes.keys(), key=len, reverse=True)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path to consistent format."""
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def _get_backend(self, path: str) -> BackendProtocol:
        """Get the appropriate backend for a path."""
        normalized = self._normalize_path(path)
        for prefix in self._sorted_prefixes:
            normalized_prefix = self._normalize_path(prefix)
            if normalized == normalized_prefix or normalized.startswith(normalized_prefix + "/"):
                return self._routes[prefix]
        return self._default

    def ls_info(self, path: str) -> list[FileInfo]:
        """List files, aggregating from all relevant backends."""
        normalized = self._normalize_path(path)

        # For root path, aggregate from all backends
        if normalized == "/":
            all_entries: dict[str, FileInfo] = {}

            # First, get entries from default backend
            for entry in self._default.ls_info(normalized):
                all_entries[entry["path"]] = entry

            # Then, add virtual directories for route prefixes
            for prefix in self._routes:
                # Extract first directory from prefix
                parts = prefix.strip("/").split("/")
                if parts[0]:
                    dir_name = parts[0]
                    dir_path = "/" + dir_name
                    if dir_path not in all_entries:
                        all_entries[dir_path] = FileInfo(
                            name=dir_name,
                            path=dir_path,
                            is_dir=True,
                            size=None,
                        )

            return sorted(all_entries.values(), key=lambda x: (not x["is_dir"], x["name"]))

        # Route to appropriate backend
        backend = self._get_backend(normalized)
        return backend.ls_info(normalized)

    def _read_bytes(self, path: str) -> bytes:
        """Read bytes from the appropriate backend."""
        return self._get_backend(path)._read_bytes(path)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read from the appropriate backend."""
        return self._get_backend(path).read(path, offset, limit)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        """Write to the appropriate backend."""
        return self._get_backend(path).write(path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit using the appropriate backend."""
        return self._get_backend(path).edit(path, old_string, new_string, replace_all)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Glob across all backends if searching from root."""
        if path == "/" or path == "":
            all_results: list[FileInfo] = []

            # Search in default backend
            all_results.extend(self._default.glob_info(pattern, path))

            # Search in each routed backend
            for prefix, backend in self._routes.items():
                results = backend.glob_info(pattern, prefix)
                all_results.extend(results)

            return sorted(all_results, key=lambda x: x["path"])

        return self._get_backend(path).glob_info(pattern, path)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> list[GrepMatch] | str:
        """Grep across all backends if no specific path."""
        if path is None or path == "/" or path == "":
            all_results: list[GrepMatch] = []

            # Search in default backend
            result = self._default.grep_raw(pattern, path, glob, ignore_hidden)
            if isinstance(result, list):
                all_results.extend(result)
            elif isinstance(result, str) and result.startswith("Error"):
                pass  # Ignore errors from individual backends

            # Search in each routed backend
            for prefix, backend in self._routes.items():
                result = backend.grep_raw(pattern, prefix, glob, ignore_hidden)
                if isinstance(result, list):
                    all_results.extend(result)

            return all_results

        return self._get_backend(path).grep_raw(pattern, path, glob, ignore_hidden)
