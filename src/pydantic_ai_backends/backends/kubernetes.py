"""Kubernetes-based sandbox for isolated command execution."""

from __future__ import annotations

import re
import time
from pathlib import PurePosixPath
from typing import Any

from pydantic_ai_backends.backends.base import BaseSandbox
from pydantic_ai_backends.types import EditResult, ExecuteResponse


class KubernetesSandbox(BaseSandbox):
    """Kubernetes-based sandbox for isolated command execution.

    Creates an ephemeral Pod in a Kubernetes cluster for running commands in an
    isolated environment. Requires the 'kubernetes' Python package.

    Example:
        ```python
        from pydantic_ai_backends import KubernetesSandbox

        # Creates a pod in the default namespace
        sandbox = KubernetesSandbox(image="python:3.11-slim")
        result = sandbox.execute("python -c 'print(1+1)'")
        print(result.output)  # "2"
        sandbox.stop()
        ```
    """

    def __init__(
        self,
        image: str = "python:3.11-slim",
        namespace: str = "default",
        kubeconfig_path: str | None = None,
        runtime_class_name: str | None = None,
        work_dir: str = "/workspace",
        sandbox_id: str | None = None,
        startup_timeout: int = 180,
    ):
        """Initialize Kubernetes sandbox.

        Args:
            image: Docker image to use for the container.
            namespace: Kubernetes namespace to create the Pod in.
            kubeconfig_path: Path to kubeconfig file. If None, tries in-cluster config first,
                             then default local kubeconfig.
            runtime_class_name: Optional RuntimeClass name for secure runtime (e.g. gvisor).
            work_dir: Working directory inside the container.
            sandbox_id: Unique identifier for this sandbox.
            startup_timeout: Seconds to wait for the Pod to become Running and Ready.
        """
        super().__init__(sandbox_id)
        self._image = image
        self._namespace = namespace
        self._kubeconfig_path = kubeconfig_path
        self._runtime_class_name = runtime_class_name
        self._work_dir = work_dir
        self._startup_timeout = startup_timeout

        self._pod_name = f"sandbox-{self._id[:12]}"
        self._v1: Any = None
        self._pod_created = False

    def _resolve_path(self, path: str) -> str:
        """Resolve relative paths against the container's working directory."""
        if not PurePosixPath(path).is_absolute():
            return str(PurePosixPath(self._work_dir) / path)
        return path

    def _ensure_client(self) -> None:
        """Ensure Kubernetes client is initialized."""
        if self._v1 is not None:
            return

        try:
            import kubernetes
            import kubernetes.client
            import kubernetes.config
        except ImportError as e:
            raise ImportError(
                "Kubernetes package not installed. "
                "Install with: pip install pydantic-ai-backend[kubernetes]"
            ) from e

        if self._kubeconfig_path:
            kubernetes.config.load_kube_config(config_file=self._kubeconfig_path)
        else:
            try:
                kubernetes.config.load_incluster_config()
            except kubernetes.config.ConfigException:
                kubernetes.config.load_kube_config()

        self._v1 = kubernetes.client.CoreV1Api()

    def _ensure_pod(self) -> None:
        """Ensure the ephemeral Pod is running and ready."""
        if self._pod_created:
            return

        self._ensure_client()
        import kubernetes.client

        # Build Pod spec
        container = kubernetes.client.V1Container(
            name="sandbox",
            image=self._image,
            command=["sh", "-c", "sleep infinity"],
            working_dir=self._work_dir,
        )

        pod_spec = kubernetes.client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
        )
        if self._runtime_class_name:
            pod_spec.runtime_class_name = self._runtime_class_name

        pod = kubernetes.client.V1Pod(
            metadata=kubernetes.client.V1ObjectMeta(
                name=self._pod_name,
                labels={"app": "pydantic-ai-sandbox", "sandbox-id": self._id},
            ),
            spec=pod_spec,
        )

        # Create Pod
        self._v1.create_namespaced_pod(namespace=self._namespace, body=pod)
        self._pod_created = True

        # Wait for Pod to be Running
        start_time = time.time()
        while time.time() - start_time < self._startup_timeout:
            try:
                pod_status = self._v1.read_namespaced_pod_status(
                    name=self._pod_name, namespace=self._namespace
                )
                if pod_status.status.phase == "Running":
                    # Extra check to ensure container is ready to accept commands
                    container_statuses = pod_status.status.container_statuses
                    if container_statuses and container_statuses[0].ready:
                        return
            except Exception:
                pass
            time.sleep(1)

        # Clean up on failure
        self.stop()
        raise RuntimeError(
            f"Kubernetes Pod {self._pod_name} failed to start within "
            f"{self._startup_timeout} seconds"
        )

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        """Execute a command in the Kubernetes Pod.

        Args:
            command: Shell command string.
            timeout: Maximum execution time in seconds.

        Returns:
            ExecuteResponse with output, exit code, and truncation status.
        """
        self._ensure_pod()
        self._last_activity = time.time()

        from kubernetes.stream import stream

        # We wrap the command to capture the exit code reliably
        # without parsing complex WebSocket channel messages.
        wrapped_command = f'cd {self._work_dir} && ({command}) ; echo -n "\n__exit_code__:$?"'

        try:
            # Note: stream library has native _timeout parameter.
            exec_args = {
                "name": self._pod_name,
                "namespace": self._namespace,
                "command": ["sh", "-c", wrapped_command],
                "stderr": True,
                "stdin": False,
                "stdout": True,
                "tty": False,
            }
            if timeout is not None:
                exec_args["_timeout"] = timeout

            output_str = stream(self._v1.connect_get_namespaced_pod_exec, **exec_args)

            if not isinstance(output_str, str):
                output_str = str(output_str)

            # Find the exit code
            match = re.search(r"__exit_code__:(\d+)\s*$", output_str)
            if match:
                exit_code = int(match.group(1))
                # Remove the appended exit code marker from the final output
                output_str = output_str[:match.start()]
            else:
                exit_code = 0

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

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file inside the Kubernetes Pod."""
        original_path = path
        path = self._resolve_path(path)
        try:
            file_bytes = self.read_bytes(path)
            if not file_bytes or file_bytes.startswith(b"[Error:"):
                return EditResult(error=f"File '{original_path}' not found")

            content = file_bytes.decode("utf-8", errors="replace")
            occurrences = content.count(old_string)

            if occurrences == 0:
                return EditResult(error="String not found in file")

            if occurrences > 1 and not replace_all:
                return EditResult(
                    error=f"String found {occurrences} times. "
                    "Use replace_all=True to replace all, or provide more context."
                )

            new_content = content.replace(old_string, new_string)
            write_result = self.write(path, new_content)

            if write_result.error:
                return EditResult(error=write_result.error)

            return EditResult(path=path, occurrences=occurrences)
        except Exception as e:
            return EditResult(error=f"Failed to edit file: {e}")

    def start(self) -> None:
        """Explicitly start the Pod."""
        self._ensure_pod()

    def is_alive(self) -> bool:
        """Check if the Pod is running."""
        if not self._pod_created:
            return False
        try:
            pod = self._v1.read_namespaced_pod_status(
                name=self._pod_name, namespace=self._namespace
            )
            return bool(pod.status.phase == "Running")
        except Exception:
            return False

    def stop(self) -> None:
        """Delete the Pod from the cluster."""
        if not self._pod_created:
            return

        try:
            import kubernetes.client

            self._v1.delete_namespaced_pod(
                name=self._pod_name,
                namespace=self._namespace,
                body=kubernetes.client.V1DeleteOptions(grace_period_seconds=0),
            )
        except Exception:
            pass
        finally:
            self._pod_created = False

    def __del__(self) -> None:
        """Cleanup Pod on garbage collection."""
        if getattr(self, "_pod_created", False):
            self.stop()
