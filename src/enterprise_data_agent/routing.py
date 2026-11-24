# 意图路由：用规则区分数据问题、闲聊、外部工具、写操作和暂不支持的请求。
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    intent: str
    confidence: float
    reason: str
    tool: str | None = None


class IntentRouter:
    """High-precision pre-router. Long-tail classification will be model-backed later."""
    def __init__(self, supported_external_tools: list[str] | None = None, mutation_verbs: list[str] | None = None, greetings: list[str] | None = None, external_tool_signals: dict[str, list[str]] | None = None, data_signals: list[str] | None = None, confidence: dict[str, float] | None = None):
        self.external_tools = set(supported_external_tools or [])
        self.mutation_verbs = [v.casefold() for v in (mutation_verbs or [])]
        self.greetings = [v.casefold() for v in (greetings or [])]
        self.external_tool_signals = {
            tool: [str(v).casefold() for v in signals]
            for tool, signals in (external_tool_signals or {}).items()
        }
        self.data_signals = [v.casefold() for v in (data_signals or [])]
        self.confidence = confidence or {}

    def _confidence(self, key: str) -> float:
        return float(self.confidence[key])

    def classify(self, question: str) -> Route:
        q = (question or "").strip()
        low = q.casefold()
        if not q:
            return Route("unsupported", self._confidence("unsupported_empty"), "empty question")
        if any(term in low for term in self.mutation_verbs):
            return Route("unsafe", self._confidence("unsafe"), "mutation request; connector is read-only")
        if low in self.greetings:
            return Route("smalltalk", self._confidence("smalltalk"), "greeting or social reply")
        for tool, signals in self.external_tool_signals.items():
            if any(signal in low for signal in signals):
                if tool in self.external_tools:
                    return Route("external_tool", self._confidence("external_tool"), "external tool signal detected", tool)
                return Route("unsupported", self._confidence("unsupported_tool"), "required external tool is not configured")
        if any(x in low for x in self.data_signals):
            return Route("data_query", self._confidence("data_query"), "business/data signal detected")
        return Route("needs_classification", self._confidence("needs_classification"), "ambiguous request; model router required")
