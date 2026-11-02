import os

from managed_deepagents import define_deep_agent

from eis_chat import ChatEIS
from tools.kibana import TOOLS

# Claude via the Elastic Inference Service, authenticated with the Elasticsearch API key (deployment secrets).
model = ChatEIS(
    es_url=os.environ["ES_URL"],
    api_key=os.environ["ES_API_KEY"],
    inference_id=os.environ.get("EIS_MODEL_ID", ".anthropic-claude-5.5-sonnet-chat_completion"),
)

agent = define_deep_agent(
    name="haven-oak-ki",
    model=model,
    tools=TOOLS,
)
