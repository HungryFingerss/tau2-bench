"""Run the benchmark with our agent registered as "fde_agent".

Takes exactly the benchmark's own command-line arguments, e.g. from the repo root:
    uv run python -m fde_agent.run run --domain retail --agent fde_agent --agent-llm ollama_chat/gpt-oss:20b --task-ids 0 --save-to my-run --verbose-logs --llm-log-mode all
(trace_viewer/record_run.py will call this for designs that use fde_agent.)
"""

import sys

from tau2.registry import registry

from fde_agent.agent import AGENT_NAME, create_fde_agent


def register() -> None:
    if registry.get_agent_factory(AGENT_NAME) is None:
        registry.register_agent_factory(create_fde_agent, AGENT_NAME)


def main(argv: list[str] | None = None):
    register()                         # before the command line is built: --agent checks names
    from tau2.cli import main as tau2_main
    sys.argv = ["tau2", *(sys.argv[1:] if argv is None else argv)]
    return tau2_main()


if __name__ == "__main__":
    sys.exit(main())
