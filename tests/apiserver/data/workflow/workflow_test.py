import os

from workflows import Context, Workflow, step
from workflows.events import StartEvent, StopEvent


class MyWorkflow(Workflow):
    @step
    def do_something(self, ctx: Context, ev: StartEvent) -> StopEvent:
        return StopEvent(result=f"Received: {ev.data}")


class _TestEnvWorkflow(Workflow):
    @step()
    async def read_env_vars(self, ctx: Context, ev: StartEvent) -> StopEvent:
        env_vars = [f"{v}: {os.environ.get(v)}" for v in ev.get("env_vars_to_read")]
        return StopEvent(result=", ".join(env_vars))
