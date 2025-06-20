import asyncio
from collections.abc import Generator
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from tenacity import RetryError

from llama_deploy.apiserver.deployment import (
    SOURCE_MANAGERS,
    Deployment,
    DeploymentError,
    Manager,
)
from llama_deploy.apiserver.deployment_config_parser import (
    DeploymentConfig,
    ServiceSource,
    SourceType,
    SyncPolicy,
    UIService,
)
from llama_deploy.control_plane import ControlPlaneConfig, ControlPlaneServer
from llama_deploy.message_queues import SimpleMessageQueue
from llama_deploy.message_queues.redis import RedisMessageQueueConfig


@pytest.fixture
def deployment_config() -> DeploymentConfig:
    return DeploymentConfig(  # type: ignore
        **{  # type: ignore
            "name": "test-deployment",
            "control-plane": ControlPlaneConfig(),
            "services": {},
        }
    )


@pytest.fixture
def mock_local_source_manager() -> Generator[mock.MagicMock, Any, None]:
    original = SOURCE_MANAGERS[SourceType.local]
    mock_sm = mock.MagicMock()
    # Make the mock class return itself when called, as the SOURCE_MANAGERS dict is a dict of factories
    mock_sm.return_value = mock_sm

    SOURCE_MANAGERS[SourceType.local] = mock_sm
    yield mock_sm
    SOURCE_MANAGERS[SourceType.local] = original


def test_deployment_ctor(data_path: Path, mock_importlib: Any, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    with mock.patch("llama_deploy.apiserver.deployment.SOURCE_MANAGERS") as sm_dict:
        sm_dict["git"] = mock.MagicMock()
        d = Deployment(config=config, base_path=data_path, deployment_path=tmp_path)

        sm_dict["git"].return_value.sync.assert_called_once()
        assert d.name == "TestDeployment"
        assert d._deployment_path.name == "TestDeployment"
        assert type(d._control_plane) is ControlPlaneServer
        assert len(d._workflow_services) == 1
        assert d.service_names == ["test-workflow"]
        assert d.client is not None
        assert d.default_service is None


def test_deployment_ctor_missing_service_path(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    config.services["test-workflow"].import_path = None
    with pytest.raises(
        ValueError, match="path field in service definition must be set"
    ):
        Deployment(config=config, base_path=data_path, deployment_path=tmp_path)


def test_deployment_ctor_missing_service_port(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    config.services["test-workflow"].port = None
    with pytest.raises(
        ValueError, match="port field in service definition must be set"
    ):
        Deployment(config=config, base_path=data_path, deployment_path=tmp_path)


def test_deployment_ctor_missing_service_host(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    config.services["test-workflow"].host = None
    with pytest.raises(
        ValueError, match="host field in service definition must be set"
    ):
        Deployment(config=config, base_path=data_path, deployment_path=tmp_path)


def test_deployment_ctor_skip_default_service(
    data_path: Path, mock_importlib: Any, tmp_path: Path
) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    config.services["test-workflow2"] = deepcopy(config.services["test-workflow"])

    with mock.patch("llama_deploy.apiserver.deployment.SOURCE_MANAGERS") as sm_dict:
        sm_dict["git"] = mock.MagicMock()
        d = Deployment(config=config, base_path=data_path, deployment_path=tmp_path)
        assert len(d._workflow_services) == 2


def test_deployment_ctor_invalid_default_service(
    data_path: Path, mock_importlib: Any, caplog: Any, tmp_path: Path
) -> None:
    config = DeploymentConfig.from_yaml(data_path / "local.yaml")
    config.default_service = "does-not-exist"

    d = Deployment(config=config, base_path=data_path, deployment_path=tmp_path)
    assert d.default_service is None
    assert (
        "There is no service with id 'does-not-exist' in this deployment, cannot set default."
        in caplog.text
    )


def test_deployment_ctor_default_service(
    data_path: Path, mock_importlib: Any, tmp_path: Path
) -> None:
    config = DeploymentConfig.from_yaml(data_path / "local.yaml")
    config.default_service = "test-workflow"

    d = Deployment(config=config, base_path=data_path, deployment_path=tmp_path)
    assert d.default_service == "test-workflow"


def test_deployment___load_message_queue_default(mocked_deployment: Deployment) -> None:
    q = mocked_deployment._load_message_queue_client(None)
    assert type(q) is SimpleMessageQueue
    assert q._config.port == 8001
    assert q._config.host == "127.0.0.1"


def test_deployment___load_message_queue_not_supported(
    mocked_deployment: Deployment,
) -> None:
    mocked_config = mock.MagicMock(queue_type="does_not_exist")
    with pytest.raises(ValueError, match="Unsupported message queue:"):
        mocked_deployment._load_message_queue_client(mocked_config)


def test_deployment__load_message_queues(mocked_deployment: Deployment) -> None:
    with mock.patch("llama_deploy.apiserver.deployment.KafkaMessageQueue") as m:
        mocked_config = mock.MagicMock(type="kafka")
        mocked_config.model_dump.return_value = {"foo": "kafka"}
        mocked_deployment._load_message_queue_client(mocked_config)
        m.assert_called_with(mocked_config)

    with mock.patch("llama_deploy.apiserver.deployment.RabbitMQMessageQueue") as m:
        mocked_config = mock.MagicMock(type="rabbitmq")
        mocked_config.model_dump.return_value = {"foo": "rabbitmq"}
        mocked_deployment._load_message_queue_client(mocked_config)
        m.assert_called_with(mocked_config)

    with mock.patch("llama_deploy.apiserver.deployment.RedisMessageQueue") as m:
        mocked_config = mock.MagicMock(type="redis")
        mocked_config.model_dump.return_value = {"foo": "redis"}
        mocked_deployment._load_message_queue_client(mocked_config)
        m.assert_called_with(mocked_config)


def test__install_dependencies(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]
    with mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp:
        # Assert the sub process cmd receives the list of dependencies
        Deployment._install_dependencies(service_config, data_path)
        called_args = [call.args[0] for call in mocked_subp.check_call.call_args_list]
        # first should've checked for uv
        assert called_args[0] == ["uv", "--version"]
        # then should install vanilla pip dependencies with uv
        assert called_args[1][:3] == ["uv", "pip", "install"]
        assert called_args[1][3].startswith("--prefix=")
        assert called_args[1][4:] == ["llama-index-core<1", "llama-index-llms-openai"]
        assert called_args[2:] == []

        # Assert the method doesn't do anything if the list of dependencies is empty
        mocked_subp.reset_mock()
        service_config.python_dependencies = []
        Deployment._install_dependencies(service_config, data_path)
        mocked_subp.check_call.assert_not_called()


def test__install_dependencies_kitchen_sink(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(
        data_path / "python_dependencies_kitchen_sink.yaml"
    )
    service_config = config.services["myworkflow"]
    with (
        mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp,
        mock.patch("llama_deploy.apiserver.deployment.os.path.isfile") as mock_isfile,
    ):
        # Mock isfile to return True for the pyproject.toml path
        def isfile_side_effect(path: str) -> bool:
            print("isfile_side_effect", path)
            return str(path).endswith("foo/bar") or "requirements.txt" in str(path)

        mock_isfile.side_effect = isfile_side_effect

        # Assert the sub process cmd receives the list of dependencies
        Deployment._install_dependencies(service_config, data_path)
        called_args = mocked_subp.check_call.call_args_list
        # first should've checked for uv
        assert called_args[0].args[0] == ["uv", "--version"]
        # then should install all things with uv pip, including requirements.txt, regular libs, and paths, assumed to be pyproject.tomls
        assert called_args[1].args[0][4:] == [
            "test<1",
            "-r",
            str(data_path / "bar/requirements.txt"),
            str(data_path / "foo/bar/"),
        ]


def test__set_environment_variables(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "env_variables.yaml")
    service_config = config.services["myworkflow"]
    with mock.patch("llama_deploy.apiserver.deployment.os.environ") as mocked_osenviron:
        # Assert the sub process cmd receives the list of dependencies
        Deployment._set_environment_variables(service_config, root=data_path)
        mocked_osenviron.__setitem__.assert_has_calls(
            [
                mock.call("VAR_1", "x"),
                mock.call("VAR_2", "y"),
                mock.call("API_KEY", "123"),
            ]
        )


def test__install_dependencies_raises(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]
    error_class = subprocess.CalledProcessError
    error = error_class(1, "cmd", output=None, stderr="There was an error")

    def raise_pip_install_error(*args: Any, **kwargs: Any) -> None:
        if args[0] == ["uv", "--version"]:
            return
        raise error

    with mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp:
        # Assert the proper exception is raised if the sub process errors out

        mocked_subp.CalledProcessError = error_class
        mocked_subp.check_call.side_effect = raise_pip_install_error
        with pytest.raises(
            DeploymentError,
            match="Unable to install service dependencies using command 'cmd': There was an error",
        ):
            Deployment._install_dependencies(service_config, data_path)


def test__install_dependencies_security_requirements_txt_path_traversal(
    data_path: Path,
) -> None:
    """Test that requirements.txt files outside source root are rejected."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]
    # Try to use a requirements.txt file outside the source root
    service_config.python_dependencies = ["../../../etc/passwd/requirements.txt"]

    with pytest.raises(
        DeploymentError,
        match="requirements file .* is not a subdirectory of the source root",
    ):
        Deployment._install_dependencies(service_config, data_path)


def test__install_dependencies_security_dependency_path_traversal(
    data_path: Path,
) -> None:
    """Test that dependency paths outside source root are rejected."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]
    # Try to use a path outside the source root
    service_config.python_dependencies = ["../../../etc/passwd/"]

    with pytest.raises(
        DeploymentError,
        match="dependency path .* is not a subdirectory of the source root",
    ):
        Deployment._install_dependencies(service_config, data_path)


def test__validate_path_is_safe_valid_paths(tmp_path: Path) -> None:
    """Test that valid paths within source root are accepted."""
    # These should not raise exceptions
    Deployment._validate_path_is_safe("./subdir/file.txt", tmp_path, "test file")
    Deployment._validate_path_is_safe("subdir/file.txt", tmp_path, "test file")
    Deployment._validate_path_is_safe("file.txt", tmp_path, "test file")


def test__validate_path_is_safe_path_traversal_attacks(tmp_path: Path) -> None:
    """Test that path traversal attacks are blocked."""
    with pytest.raises(
        DeploymentError, match="test file .* is not a subdirectory of the source root"
    ):
        Deployment._validate_path_is_safe("../../../etc/passwd", tmp_path, "test file")

    with pytest.raises(
        DeploymentError, match="test file .* is not a subdirectory of the source root"
    ):
        Deployment._validate_path_is_safe("../../outside.txt", tmp_path, "test file")

    with pytest.raises(
        DeploymentError, match="test file .* is not a subdirectory of the source root"
    ):
        Deployment._validate_path_is_safe("../outside.txt", tmp_path, "test file")


def test__validate_path_is_safe_absolute_paths(tmp_path: Path) -> None:
    """Test that absolute paths outside source root are blocked."""
    with pytest.raises(
        DeploymentError, match="test file .* is not a subdirectory of the source root"
    ):
        Deployment._validate_path_is_safe("/etc/passwd", tmp_path, "test file")

    with pytest.raises(
        DeploymentError, match="test file .* is not a subdirectory of the source root"
    ):
        Deployment._validate_path_is_safe("/tmp/outside.txt", tmp_path, "test file")


def test__install_dependencies_uv_not_available_bootstrap_success(
    data_path: Path,
) -> None:
    """Test uv bootstrap when uv is not initially available but pip install succeeds."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]

    def mock_check_call(*args: Any, **kwargs: Any) -> None:
        if args[0] == ["uv", "--version"]:
            # First call to check uv version fails
            raise subprocess.CalledProcessError(1, "uv --version")
        elif args[0][:4] == [sys.executable, "-m", "pip", "install"]:
            # pip install uv succeeds
            return
        elif args[0][:3] == ["uv", "pip", "install"]:
            # uv pip install succeeds after bootstrap
            return
        else:
            raise subprocess.CalledProcessError(1, str(args[0]))

    with mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp:
        mocked_subp.CalledProcessError = subprocess.CalledProcessError
        mocked_subp.check_call.side_effect = mock_check_call
        mocked_subp.DEVNULL = subprocess.DEVNULL

        # Should not raise an exception
        Deployment._install_dependencies(service_config, data_path)

        # Verify the bootstrap sequence
        calls = mocked_subp.check_call.call_args_list
        assert len(calls) == 3
        # First: check uv version (fails)
        assert calls[0].args[0] == ["uv", "--version"]
        # Second: bootstrap uv with pip
        assert calls[1].args[0][:4] == [sys.executable, "-m", "pip", "install"]
        assert "uv" in calls[1].args[0]
        # Third: install dependencies with uv
        assert calls[2].args[0][:3] == ["uv", "pip", "install"]


def test__install_dependencies_uv_not_available_bootstrap_fails(
    data_path: Path,
) -> None:
    """Test uv bootstrap failure when pip install uv fails."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]

    def mock_check_call(*args: Any, **kwargs: Any) -> None:
        if args[0] == ["uv", "--version"]:
            # uv not available
            raise subprocess.CalledProcessError(1, "uv --version")
        elif args[0][:4] == [sys.executable, "-m", "pip", "install"]:
            # pip install uv fails
            error = subprocess.CalledProcessError(
                1, "pip install uv", stderr="Bootstrap failed"
            )
            raise error
        else:
            raise subprocess.CalledProcessError(1, str(args[0]))

    with mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp:
        mocked_subp.CalledProcessError = subprocess.CalledProcessError
        mocked_subp.check_call.side_effect = mock_check_call
        mocked_subp.DEVNULL = subprocess.DEVNULL

        with pytest.raises(
            DeploymentError,
            match="Unable to install uv. Environment must include uv, or uv must be installed with pip:",
        ):
            Deployment._install_dependencies(service_config, data_path)


def test__install_dependencies_uv_not_available_file_not_found(data_path: Path) -> None:
    """Test when uv command is not found at all."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]

    def mock_check_call(*args: Any, **kwargs: Any) -> None:
        if args[0] == ["uv", "--version"]:
            # uv command not found
            raise FileNotFoundError("uv command not found")
        elif args[0][:4] == [sys.executable, "-m", "pip", "install"]:
            # pip install uv succeeds
            return
        elif args[0][:3] == ["uv", "pip", "install"]:
            # uv pip install succeeds after bootstrap
            return
        else:
            raise subprocess.CalledProcessError(1, str(args[0]))

    with mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp:
        mocked_subp.CalledProcessError = subprocess.CalledProcessError
        mocked_subp.check_call.side_effect = mock_check_call
        mocked_subp.DEVNULL = subprocess.DEVNULL

        # Should not raise an exception, should bootstrap uv
        Deployment._install_dependencies(service_config, data_path)

        # Verify the bootstrap sequence
        calls = mocked_subp.check_call.call_args_list
        assert len(calls) == 3
        # First: check uv version (FileNotFoundError)
        assert calls[0].args[0] == ["uv", "--version"]
        # Second: bootstrap uv with pip
        assert calls[1].args[0][:4] == [sys.executable, "-m", "pip", "install"]
        # Third: install dependencies with uv
        assert calls[2].args[0][:3] == ["uv", "pip", "install"]


def test__install_dependencies_mixed_path_types(
    data_path: Path, tmp_path: Path
) -> None:
    """Test handling of mixed dependency types including paths with different separators."""
    config = DeploymentConfig.from_yaml(data_path / "python_dependencies.yaml")
    service_config = config.services["myworkflow"]

    # Create test files in the source root
    test_req_file = tmp_path / "test_requirements.txt"
    test_req_file.write_text("requests>=2.0.0\n")
    test_pyproject_dir = tmp_path / "test_project"
    test_pyproject_dir.mkdir()
    (test_pyproject_dir / "pyproject.toml").write_text("[project]\nname = 'test'\n")

    # Mix of different dependency types
    service_config.python_dependencies = [
        "regular-package>=1.0.0",  # Regular package
        "./test_requirements.txt",  # Requirements file
        "./test_project/",  # Directory with pyproject.toml
        "package-with-version<2.0",  # Package with version constraint
    ]

    with (
        mock.patch("llama_deploy.apiserver.deployment.subprocess") as mocked_subp,
        mock.patch("llama_deploy.apiserver.deployment.os.path.isfile") as mock_isfile,
    ):

        def isfile_side_effect(path: str) -> bool:
            return str(path).endswith("test_project") or "test_requirements.txt" in str(
                path
            )

        mock_isfile.side_effect = isfile_side_effect

        Deployment._install_dependencies(service_config, tmp_path)

        calls = mocked_subp.check_call.call_args_list
        # Should have uv version check and install command
        assert len(calls) == 2
        assert calls[0].args[0] == ["uv", "--version"]

        # Check the install arguments
        install_args = calls[1].args[0][4:]  # Skip "uv pip install --prefix=..."
        expected_args = [
            "regular-package>=1.0.0",
            "-r",
            str(tmp_path / "test_requirements.txt"),
            str(tmp_path / "test_project/"),
            "package-with-version<2.0",
        ]
        assert install_args == expected_args


def test_manager_ctor() -> None:
    m = Manager()
    m.set_deployments_path(None)
    assert m.deployments_path.name == "deployments"
    assert m._max_deployments == 10

    m = Manager(max_deployments=42)
    m.set_deployments_path(None)
    assert m.deployments_path.name == "deployments"
    assert m._max_deployments == 42


@pytest.mark.asyncio
async def test_manager_deploy_duplicate(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")

    m = Manager()
    m._serving = True
    m._deployments["TestDeployment"] = mock.MagicMock()

    with pytest.raises(ValueError, match="Deployment already exists: TestDeployment"):
        await m.deploy(config, base_path=str(data_path))


@pytest.mark.asyncio
async def test_manager_deploy_maximum_reached(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")

    m = Manager(max_deployments=1)
    m._serving = True
    m._deployments["AnotherDeployment"] = mock.MagicMock()

    with pytest.raises(
        ValueError,
        match="Reached the maximum number of deployments, cannot schedule more",
    ):
        await m.deploy(config, base_path="")


@pytest.mark.asyncio
async def test_manager_deploy(data_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    # Do not use SimpleMessageQueue here, to avoid starting the server
    config.message_queue = RedisMessageQueueConfig()

    with mock.patch(
        "llama_deploy.apiserver.deployment.Deployment"
    ) as mocked_deployment:
        m = Manager()
        m._serving = True
        m._deployments_path = Path()
        await m.deploy(config, base_path=str(data_path))
        mocked_deployment.assert_called_once()
        assert m.deployment_names == ["TestDeployment"]
        assert m.get_deployment("TestDeployment") is not None


@pytest.mark.asyncio
async def test_manager_serve_loop(tmp_path: Path) -> None:
    m = Manager()
    m.set_deployments_path(tmp_path)
    serve_task = asyncio.create_task(m.serve())
    # Allow the serve task to start
    await asyncio.sleep(0)

    # Check that the task is still running
    assert not serve_task.done()

    # Cancel the task
    serve_task.cancel()
    await serve_task
    assert serve_task.done()
    assert serve_task.exception() is None


def test_manager_assign_control_plane_port(data_path: Path) -> None:
    m = Manager()
    config = DeploymentConfig.from_yaml(data_path / "service_ports.yaml")
    m._assign_control_plane_address(config)
    assert config.services["no-port"].port == 8002
    assert config.services["has-port"].port == 9999
    assert config.services["no-port-again"].port == 8003


@pytest.mark.asyncio
async def test_start_control_plane_success(
    deployment_config: DeploymentConfig, tmp_path: Path
) -> None:
    # Create deployment instance
    deployment = Deployment(
        config=deployment_config, base_path=Path(), deployment_path=tmp_path
    )

    # Mock control plane methods
    deployment._control_plane.launch_server = mock.AsyncMock()  # type: ignore

    # Mock httpx client
    mock_response = mock.MagicMock()
    mock_response.raise_for_status = mock.MagicMock()

    mock_client = mock.AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with mock.patch("httpx.AsyncClient", return_value=mock_client):
        # Run the method
        tasks = await deployment._start_control_plane()

        # Verify tasks were created
        assert len(tasks)
        assert all(isinstance(task, asyncio.Task) for task in tasks)

        # Verify control plane methods were called
        deployment._control_plane.launch_server.assert_called_once()

        # Verify health check was performed
        mock_client.__aenter__.return_value.get.assert_called_with(
            deployment_config.control_plane.url
        )


@pytest.mark.asyncio
async def test_start_control_plane_failure(
    deployment_config: DeploymentConfig, tmp_path: Path
) -> None:
    # Create deployment instance
    deployment = Deployment(
        config=deployment_config, base_path=Path(), deployment_path=tmp_path
    )

    # Mock control plane methods
    deployment._control_plane.launch_server = mock.AsyncMock()  # type: ignore

    # Create a mock attempt
    mock_attempt: asyncio.Future = asyncio.Future()
    mock_attempt.set_exception(Exception("Connection failed"))

    # Mock AsyncRetrying to raise an exception
    with mock.patch(
        "llama_deploy.apiserver.deployment.AsyncRetrying",
        side_effect=RetryError(last_attempt=mock_attempt),  # type: ignore
    ):
        # Verify DeploymentError is raised
        with pytest.raises(DeploymentError) as exc_info:
            await deployment._start_control_plane()

        assert "Unable to reach Control Plane" in str(exc_info.value)

        # Verify control plane methods were still called
        deployment._control_plane.launch_server.assert_called_once()


@pytest.mark.asyncio
async def test_start_sequence(
    deployment_config: DeploymentConfig, tmp_path: Path
) -> None:
    deployment = Deployment(
        config=deployment_config, base_path=Path(), deployment_path=tmp_path
    )
    deployment._start_control_plane = mock.AsyncMock()  # type: ignore
    deployment._run_services = mock.AsyncMock()  # type: ignore
    deployment._start_ui_server = mock.AsyncMock()  # type: ignore
    await deployment.start()
    deployment._start_control_plane.assert_awaited_once()
    # no services should start
    deployment._run_services.assert_not_awaited()
    # no ui server
    deployment._start_ui_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_with_services(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    with mock.patch("llama_deploy.apiserver.deployment.SOURCE_MANAGERS") as sm_dict:
        deployment = Deployment(
            config=config, base_path=data_path, deployment_path=tmp_path
        )
        deployment._start_control_plane = mock.AsyncMock(return_value=[])  # type: ignore
        deployment._run_services = mock.AsyncMock(return_value=[])  # type: ignore
        deployment._start_ui_server = mock.AsyncMock(return_value=[])  # type: ignore

        sm_dict["git"] = mock.MagicMock()
        await deployment.start()
        sm_dict["git"].return_value.sync.assert_called_once()

    # Verify control plane was started
    deployment._start_control_plane.assert_awaited_once()

    # Verify services were started
    deployment._run_services.assert_awaited_once()

    # Verify UI server was not started
    deployment._start_ui_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_with_services_ui(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "git_service.yaml")
    config.ui = mock.MagicMock()
    with mock.patch("llama_deploy.apiserver.deployment.SOURCE_MANAGERS") as sm_dict:
        deployment = Deployment(
            config=config, base_path=data_path, deployment_path=tmp_path
        )
        deployment._start_control_plane = mock.AsyncMock(return_value=[])  # type: ignore
        deployment._run_services = mock.AsyncMock(return_value=[])  # type: ignore
        deployment._start_ui_server = mock.AsyncMock(return_value=[])  # type: ignore

        sm_dict["git"] = mock.MagicMock()
        await deployment.start()

    deployment._start_control_plane.assert_awaited_once()
    deployment._run_services.assert_awaited_once()
    deployment._start_ui_server.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_ui_server_success(data_path: Path, tmp_path: Path) -> None:
    config = DeploymentConfig.from_yaml(data_path / "with_ui.yaml")
    deployment = Deployment(
        config=config, base_path=data_path, deployment_path=tmp_path
    )

    # Mock the necessary components
    with (
        mock.patch("llama_deploy.apiserver.deployment.SOURCE_MANAGERS") as sm_dict,
        mock.patch(
            "llama_deploy.apiserver.deployment.asyncio.create_subprocess_exec"
        ) as mock_subprocess,
        mock.patch("llama_deploy.apiserver.deployment.os") as mock_os,
    ):
        # Configure source manager mock
        source_manager_mock = mock.MagicMock()
        sm_dict.__getitem__.return_value.return_value = source_manager_mock

        # Configure subprocess mock
        process_mock = mock.AsyncMock()
        process_mock.pid = 12345
        mock_subprocess.return_value = process_mock
        process_mock.wait.return_value = 0

        # Configure os environment
        mock_os.environ.copy.return_value = {"PATH": "/some/path"}

        # Run the method
        await deployment._start_ui_server()

        # Verify source manager was used correctly
        source_manager_mock.sync.assert_called_once()

        # Verify npm commands were executed
        assert mock_subprocess.call_count == 2
        # First call should be npm ci
        assert mock_subprocess.call_args_list[0][0][:2] == ("pnpm", "install")
        # Second call should be npm run dev
        assert mock_subprocess.call_args_list[1][0][:3] == ("pnpm", "run", "dev")

        # Verify environment variables were set
        assert mock_os.environ.copy.called
        env = mock_subprocess.call_args_list[1][1]["env"]
        assert env["LLAMA_DEPLOY_NEXTJS_BASE_PATH"] == "/deployments/test-deployment/ui"
        assert env["LLAMA_DEPLOY_NEXTJS_DEPLOYMENT_NAME"] == "test-deployment"


@pytest.mark.asyncio
async def test_start_ui_server_missing_config(
    deployment_config: DeploymentConfig, tmp_path: Path
) -> None:
    """Test that _start_ui_server raises appropriate error when UI config is missing."""
    deployment_config.ui = None
    deployment = Deployment(
        config=deployment_config, base_path=Path(), deployment_path=tmp_path
    )

    with pytest.raises(ValueError, match="missing ui configuration settings"):
        await deployment._start_ui_server()


@pytest.mark.asyncio
async def test_merges_in_path_to_installation(
    deployment_config: DeploymentConfig, tmp_path: Path
) -> None:
    """Test that _start_ui_server raises appropriate error when source is missing."""
    deployment_config.ui = mock.MagicMock(source=None)
    deployment = Deployment(
        config=deployment_config, base_path=Path(), deployment_path=tmp_path
    )

    with pytest.raises(ValueError, match="source must be defined"):
        await deployment._start_ui_server()


@pytest.mark.asyncio
async def test_start_ui_server_uses_merge_sync_policy(
    deployment_config: DeploymentConfig,
    tmp_path: Path,
    mock_local_source_manager: mock.MagicMock,
) -> None:
    """Test that _start_ui_server syncs into the correct directory, and starts services there"""
    # Setup a mock source with sync_policy set to MERGE
    mock_source = ServiceSource(
        type=SourceType.local,
        location="some/location",
        sync_policy=SyncPolicy.MERGE,
    )
    deployment_config.ui = UIService.model_validate(
        {
            "name": "test-ui",
            "source": mock_source,
        }
    )
    # Patch SOURCE_MANAGERS to return a mock source manager
    mock_local_source_manager.relative_path = mock.MagicMock()
    mock_local_source_manager.relative_path.return_value = "some/location"

    # Patch subprocess and os
    mock_subprocess = mock.AsyncMock()
    with mock.patch("asyncio.create_subprocess_exec", mock_subprocess):
        mock_subprocess.return_value.wait = mock.AsyncMock()
        mock_subprocess.return_value.pid = 1234

        deployment = Deployment(
            config=deployment_config, base_path=Path(), deployment_path=tmp_path
        )

        await deployment._start_ui_server()

        # Check that sync was called with SyncPolicy.MERGE
        assert mock_local_source_manager.sync.call_count == 1
        args, kwargs = mock_local_source_manager.sync.call_args
        # The third argument is the policy
        assert args[2] == SyncPolicy.MERGE
        # verify that the relative path was used for the commands
        installed_path = tmp_path / "test-deployment" / "some/location"
        # The first call to create_subprocess_exec should be for "pnpm", "install"
        install_call = mock_subprocess.call_args_list[0]
        assert install_call.args[:2] == ("pnpm", "install")
        assert install_call.kwargs["cwd"] == installed_path

        # The second call to create_subprocess_exec should be for "pnpm", "run", "dev"
        run_call = mock_subprocess.call_args_list[1]
        assert run_call.args[:3] == ("pnpm", "run", "dev")
        assert run_call.kwargs["cwd"] == installed_path
