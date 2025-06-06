import asyncio

import httpx
from llama_index.core.workflow import Workflow
from pydantic_settings import BaseSettings

from llama_deploy.control_plane.server import ControlPlaneConfig, ControlPlaneServer
from llama_deploy.message_queues import (
    AbstractMessageQueue,
    KafkaMessageQueue,
    KafkaMessageQueueConfig,
    RabbitMQMessageQueue,
    RabbitMQMessageQueueConfig,
    RedisMessageQueue,
    RedisMessageQueueConfig,
    SimpleMessageQueueConfig,
    SimpleMessageQueueServer,
)
from llama_deploy.message_queues.simple import SimpleMessageQueue
from llama_deploy.services.network_service_manager import NetworkServiceManager
from llama_deploy.services.workflow import WorkflowService, WorkflowServiceConfig

DEFAULT_TIMEOUT = 120.0


def _get_message_queue_config(config_dict: dict) -> BaseSettings:
    key = next(iter(config_dict.keys()))
    if key == SimpleMessageQueueConfig.__name__:
        return SimpleMessageQueueConfig(**config_dict[key])
    elif key == KafkaMessageQueueConfig.__name__:
        return KafkaMessageQueueConfig(**config_dict[key])
    elif key == RabbitMQMessageQueueConfig.__name__:
        return RabbitMQMessageQueueConfig(**config_dict[key])
    elif key == RedisMessageQueueConfig.__name__:
        return RedisMessageQueueConfig(**config_dict[key])
    else:
        raise ValueError(f"Unknown message queue: {key}")


def _get_message_queue_client(config: BaseSettings) -> AbstractMessageQueue:
    if isinstance(config, SimpleMessageQueueConfig):
        return SimpleMessageQueue(config)
    elif isinstance(config, KafkaMessageQueueConfig):
        return KafkaMessageQueue(config)
    elif isinstance(config, RabbitMQMessageQueueConfig):
        return RabbitMQMessageQueue(config)
    elif isinstance(config, RedisMessageQueueConfig):
        return RedisMessageQueue(config)
    else:
        raise ValueError(f"Invalid message queue config: {config}")


async def deploy_core(
    control_plane_config: ControlPlaneConfig | None = None,
    message_queue_config: BaseSettings | None = None,
    disable_message_queue: bool = False,
    disable_control_plane: bool = False,
) -> None:
    """
    Deploy the core components of the llama_deploy system.

    This function sets up and launches the message queue, control plane, and orchestrator.
    It handles the initialization and connection of these core components.

    Args:
        control_plane_config (Optional[ControlPlaneConfig]): Configuration for the control plane.
        message_queue_config (Optional[BaseSettings]): Configuration for the message queue. Defaults to a local SimpleMessageQueue.
        disable_message_queue (bool): Whether to disable deploying the message queue. Defaults to False.
        disable_control_plane (bool): Whether to disable deploying the control plane. Defaults to False.

    Raises:
        ValueError: If an unknown message queue type is specified in the config.
        Exception: If any of the launched tasks encounter an error.
    """
    control_plane_config = control_plane_config or ControlPlaneConfig()
    message_queue_config = message_queue_config or SimpleMessageQueueConfig()

    tasks = []

    message_queue_client = _get_message_queue_client(message_queue_config)
    # If needed, start the SimpleMessageQueueServer
    if (
        isinstance(message_queue_config, SimpleMessageQueueConfig)
        and not disable_message_queue
    ):
        queue = SimpleMessageQueueServer(message_queue_config)
        tasks.append(asyncio.create_task(queue.launch_server()))
        # let message queue boot up
        await asyncio.sleep(2)

    if not disable_control_plane:
        control_plane = ControlPlaneServer(
            message_queue_client, config=control_plane_config
        )
        tasks.append(asyncio.create_task(control_plane.launch_server()))
        # let service spin up
        await asyncio.sleep(4)

    # let things run
    try:
        await asyncio.gather(*tasks)
    except (Exception, asyncio.CancelledError):
        await message_queue_client.cleanup()
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)


async def deploy_workflow(
    workflow: Workflow,
    workflow_config: WorkflowServiceConfig,
    control_plane_config: ControlPlaneConfig | None = None,
) -> None:
    """
    Deploy a workflow as a service within the llama_deploy system.

    This function sets up a workflow as a service, connects it to the message queue,
    and registers it with the control plane.

    Args:
        workflow (Workflow): The workflow to be deployed as a service.
        workflow_config (WorkflowServiceConfig): Configuration for the workflow service.
        control_plane_config (Optional[ControlPlaneConfig]): Configuration for the control plane.

    Raises:
        httpx.HTTPError: If there's an error communicating with the control plane.
        ValueError: If an invalid message queue config is encountered.
        Exception: If any of the launched tasks encounter an error.
    """
    control_plane_config = control_plane_config or ControlPlaneConfig()
    control_plane_url = control_plane_config.url

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{control_plane_url}/queue_config")
        queue_config_dict = response.json()

    message_queue_config = _get_message_queue_config(queue_config_dict)
    message_queue_client = _get_message_queue_client(message_queue_config)

    # override the service manager, while maintaining dict of existing services
    workflow._service_manager = NetworkServiceManager(
        workflow._service_manager._services
    )

    service = WorkflowService(
        workflow=workflow,
        message_queue=message_queue_client,
        config=workflow_config,
    )

    # register to control plane
    await service.register_to_control_plane(control_plane_url)
    await asyncio.sleep(1)

    await service.launch_server()
