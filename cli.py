# AgentCore Platform v1.0
# Framework Design & Engineering (FDE)
"""AGENTIC STAR Marketplace entrypoint — one-shot Pod process.

Referenced by the Marketplace Dockerfile's CMD. Compiles the agent, provisions
its secrets, then hands off to shared.bootstrap.marketplace_app for the
Marketplace lifecycle (identity, input, events, terminal delivery, exit).

配布物の `agenticstar-deploy/cli.py` は使わない。あちらは
`from graph.graph import Graph` と書いており、本テンプレートのレイアウト
（`src/graph/graph.py` + Dockerfile の `PYTHONPATH=/app`）では import できない。
`build_agent.sh` はエージェント自身の `cli.py` を優先するため、本ファイルが採用される。

`agent_name` / `namespace` は `config/agent.yaml` の `id` / `namespace` と一致させる。
"""

from src.graph.graph import Graph
from shared.bootstrap.marketplace_app import run_agent_marketplace


def main() -> None:
    run_agent_marketplace(
        Graph,
        agent_name="TRV-C2-019",
        namespace="trv",
    )


if __name__ == "__main__":
    main()
