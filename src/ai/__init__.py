"""The model-facing code: what asks a model, and what reads its answer.

`agents.py` is the transport and the two adjusters built on it. `tool_agent.py`
is the LangGraph agent behind the chat bubble, renamed from `agent.py` because
one letter was the only thing distinguishing it and neither name said which was
which.

Prompts stay in src/prompts/: components/ and workflows/ read them too.
"""
