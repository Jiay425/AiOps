"""Graph exports for LangGraph CLI/Studio."""

from .config import get_settings
from .graphs import CodeOpsGraph, OpsDiagnosisGraph
from .llm import OpenAICompatibleClient
from .tools import ObservabilityTools
from .store import Store

_settings = get_settings()
_llm = OpenAICompatibleClient(_settings)
_store = Store(_settings.ops_database_path)

ops_diagnosis_graph = OpsDiagnosisGraph(ObservabilityTools(_settings), _llm, _store).graph
codeops_graph = CodeOpsGraph(_llm, _store).graph
