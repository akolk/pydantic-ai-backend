"""Tests for KubernetesSandbox with fully mocked Kubernetes API."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fake Kubernetes SDK objects
# ---------------------------------------------------------------------------

@dataclass
class FakeV1Container:
    name: str
    image: str
    command: list[str]
    working_dir: str


@dataclass
class FakeV1PodSpec:
    containers: list[FakeV1Container]
    restart_policy: str
    runtime_class_name: str | None = None


@dataclass
class FakeV1ObjectMeta:
    name: str
    labels: dict[str, str]


@dataclass
class FakeV1Pod:
    metadata: FakeV1ObjectMeta
    spec: FakeV1PodSpec


@dataclass
class FakeContainerStatus:
    ready: bool


@dataclass
class FakePodStatusDetails:
    phase: str
    container_statuses: list[FakeContainerStatus]


@dataclass
class FakePodStatus:
    status: FakePodStatusDetails


class FakeCoreV1Api:
    """Mock for CoreV1Api."""

    def __init__(self) -> None:
        self.created_pods: list[FakeV1Pod] = []
        self.deleted_pods: list[str] = []
        self.pod_phase = "Running"
        self.container_ready = True

    def create_namespaced_pod(self, namespace: str, body: FakeV1Pod) -> FakeV1Pod:
        self.created_pods.append(body)
        return body

    def read_namespaced_pod_status(self, name: str, namespace: str) -> FakePodStatus:
        return FakePodStatus(
            status=FakePodStatusDetails(
                phase=self.pod_phase,
                container_statuses=[FakeContainerStatus(ready=self.container_ready)],
            )
        )

    def delete_namespaced_pod(self, name: str, namespace: str, body: Any = None) -> None:
        self.deleted_pods.append(name)

    def connect_get_namespaced_pod_exec(self, *args: Any, **kwargs: Any) -> Any:
        pass


class FakeConfig:
    @staticmethod
    def load_kube_config(config_file: str | None = None) -> None:
        pass

    @staticmethod
    def load_incluster_config() -> None:
        pass


# ---------------------------------------------------------------------------
# Setup Mock Module
# ---------------------------------------------------------------------------

_MOCK_STREAM_RESULT = "hello world\n__exit_code__:0\n"


def mock_stream(*args: Any, **kwargs: Any) -> str:
    return _MOCK_STREAM_RESULT


_FAKE_KUBERNETES_CLIENT = MagicMock(
    CoreV1Api=FakeCoreV1Api,
    V1Container=FakeV1Container,
    V1PodSpec=FakeV1PodSpec,
    V1ObjectMeta=FakeV1ObjectMeta,
    V1Pod=FakeV1Pod,
    V1DeleteOptions=MagicMock(),
)

_FAKE_KUBERNETES_MODULE = MagicMock(
    client=_FAKE_KUBERNETES_CLIENT,
    config=FakeConfig,
)


@pytest.fixture(autouse=True)
def _mock_k8s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake 'kubernetes' module into sys.modules for every test."""
    monkeypatch.setitem(sys.modules, "kubernetes", _FAKE_KUBERNETES_MODULE)
    monkeypatch.setitem(sys.modules, "kubernetes.client", _FAKE_KUBERNETES_CLIENT)
    monkeypatch.setitem(sys.modules, "kubernetes.config", FakeConfig)
    monkeypatch.setitem(sys.modules, "kubernetes.stream", MagicMock(stream=mock_stream))


def _make_sandbox(**kwargs: Any) -> Any:
    """Create a KubernetesSandbox with mocked environment."""
    from pydantic_ai_backends.backends.kubernetes import KubernetesSandbox

    return KubernetesSandbox(**kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestKubernetesSandboxInit:
    def test_init_basic(self) -> None:
        sandbox = _make_sandbox()
        assert sandbox._image == "python:3.11-slim"
        assert sandbox._namespace == "default"
        assert sandbox._pod_created is False

    def test_custom_params(self) -> None:
        sandbox = _make_sandbox(
            image="custom-python:3.12",
            namespace="my-ns",
            runtime_class_name="gvisor",
            work_dir="/app",
        )
        assert sandbox._image == "custom-python:3.12"
        assert sandbox._namespace == "my-ns"
        assert sandbox._runtime_class_name == "gvisor"
        assert sandbox._work_dir == "/app"


class TestKubernetesSandboxLifecycle:
    def test_start_creates_pod(self) -> None:
        sandbox = _make_sandbox()
        sandbox.start()
        assert sandbox._pod_created is True
        assert len(sandbox._v1.created_pods) == 1
        assert sandbox._v1.created_pods[0].metadata.name == sandbox._pod_name

    def test_startup_timeout_raises(self) -> None:
        from pydantic_ai_backends.backends.kubernetes import KubernetesSandbox

        # Create a mock API that simulates Pod starting up but never becoming ready
        mock_api = FakeCoreV1Api()
        mock_api.container_ready = False

        with patch.object(_FAKE_KUBERNETES_CLIENT, "CoreV1Api", return_value=mock_api):
            sandbox = KubernetesSandbox(startup_timeout=1)
            with pytest.raises(RuntimeError, match="failed to start"):
                sandbox.start()

    def test_stop_deletes_pod(self) -> None:
        sandbox = _make_sandbox()
        sandbox.start()
        sandbox.stop()
        assert sandbox._pod_created is False
        assert len(sandbox._v1.deleted_pods) == 1
        assert sandbox._v1.deleted_pods[0] == sandbox._pod_name

    def test_is_alive_true(self) -> None:
        sandbox = _make_sandbox()
        sandbox.start()
        assert sandbox.is_alive() is True

    def test_is_alive_false(self) -> None:
        sandbox = _make_sandbox()
        assert sandbox.is_alive() is False


class TestKubernetesSandboxExecute:
    def test_execute_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = _make_sandbox()
        sandbox.start()

        # Mock stream response to return simple echo with exit code
        monkeypatch.setitem(
            sys.modules,
            "kubernetes.stream",
            MagicMock(stream=lambda *a, **kw: "success message\n__exit_code__:0\n"),
        )

        resp = sandbox.execute("echo test")
        assert resp.exit_code == 0
        assert resp.output == "success message\n"
        assert resp.truncated is False

    def test_execute_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = _make_sandbox()
        sandbox.start()

        monkeypatch.setitem(
            sys.modules,
            "kubernetes.stream",
            MagicMock(stream=lambda *a, **kw: "error output\n__exit_code__:127\n"),
        )

        resp = sandbox.execute("invalid-command")
        assert resp.exit_code == 127
        assert resp.output == "error output\n"

    def test_execute_timeout_handling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = _make_sandbox()
        sandbox.start()

        def mock_stream_timeout(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("timeout reached")

        monkeypatch.setitem(
            sys.modules,
            "kubernetes.stream",
            MagicMock(stream=mock_stream_timeout),
        )

        resp = sandbox.execute("sleep 10", timeout=1)
        assert resp.exit_code == 1
        assert "timeout reached" in resp.output


class TestKubernetesSandboxLazyImport:
    def test_lazy_import_from_package(self) -> None:
        import pydantic_ai_backends

        cls = pydantic_ai_backends.KubernetesSandbox
        assert cls.__name__ == "KubernetesSandbox"

    def test_in_all(self) -> None:
        import pydantic_ai_backends

        assert "KubernetesSandbox" in pydantic_ai_backends.__all__
