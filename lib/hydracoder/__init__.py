"""hydracoder: local-AI development orchestrator.

A web UI + orchestrator that turns a project goal into a task graph and drives
local models (planner, worker, reviewer, boss roles) to build it, with a
resilient journal so a crash or full context never loses work. Built on
hydra-llm (model substrate) and lillycoder (agent loop).

hydracoder is provided AS IS, WITHOUT WARRANTY OF ANY KIND. It runs local
models that read, write, and delete files in a workspace and run shell
commands. You alone are responsible for any damage to your data, hardware, or
system. By running it you accept all risk.
"""
__version__ = "0.1.0"
