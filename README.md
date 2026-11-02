# Haven & Oak catalog agents (LangSmith Managed Deep Agents)

Two deployable agents for the Haven & Oak furniture-retail context-engine demo. They share the same
model, runtime and Kibana tools, and differ in the system prompt and tool list.

| | `haven-oak-baseline` | `haven-oak-ki` |
|---|---|---|
| Idea | Discover the data with tools; no pre-computed knowledge | Retrieve pre-computed Knowledge Indicators (KIs) from a Context Engine AI index first |
| System prompt | `instructions.md` (about 1.8k characters) | `instructions.md` (about 5.4k characters) |
| Kibana MCP tools | 9 (includes list and describe AI index) | 7 (no list or describe AI index) |
| AI index | none named in the prompt | `catalog-optimizer` (`ai-index-idx-catalog-optimizer`), the only one it is told to use |

Model: Claude Sonnet 5.5 through the Elastic Inference Service (`.anthropic-claude-5.5-sonnet-chat_completion`),
authenticated with the Elasticsearch API key (`eis_chat.py`). Override with `EIS_MODEL_ID`.

Tools: `tools/kibana.py` wraps the Kibana Agent Builder MCP endpoint as custom Python tools, because managed
`define_mcp` cannot send a static `Authorization: ApiKey` header. Tool names, descriptions and schemas were copied
from the live MCP server.

## Data the agents query

Four Elasticsearch indices: `furniture-products`, `furniture-query-logs`, `furniture-purchase-logs`,
`furniture-reviews`. The `ki` agent also reads the `catalog-optimizer` AI index, which holds index-metadata,
per-product metric and review-theme KIs.

## Deploy

Each directory is its own project. Copy `.env.example` to `.env` and fill it in (never commit `.env`):

```
LANGSMITH_API_KEY=   # authenticates the mda CLI; not uploaded
ES_URL=              # Elasticsearch URL
ES_API_KEY=          # Elasticsearch API key (also used for EIS and the Kibana MCP calls)
KIBANA_MCP_URL=      # https://<kibana>/api/agent_builder/mcp
```

```bash
cd haven-oak-ki
uvx --from managed-deepagents==0.9.5 mda deploy
```

Deploying again updates the agent by name. Call a deployed agent with `langgraph_sdk` and your LangSmith key
(`x-api-key`).

## Notes

- `instructions.md` for the `ki` agent was generated from the benchmark code (`ki_agent.py` in the source
  project); this repo holds the generated output only.
- To point the `ki` agent at another AI index, change the index name in `instructions.md`.
- Results from the benchmark runs are not part of this repo.
