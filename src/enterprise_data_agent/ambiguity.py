# 歧义检测：角色或指标口径不明确时返回澄清问题，不擅自选定字段。
from __future__ import annotations
from typing import Any


class AmbiguityDetector:
    def __init__(self, ontology: dict[str, Any]):
        self.cases = ontology.get("ambiguity_cases", [])
        self.dimensions = ontology.get("dimensions", [])

    def detect(self, question: str) -> dict[str, Any] | None:
        q = (question or "").casefold()
        for case in self.cases:
            value = case.get("value")
            if value and str(value).casefold() in q:
                roles = case.get("candidate_roles", [])
                role_aliases = {
                    str(d.get("id")): [str(a).casefold() for a in d.get("aliases", [])]
                    for d in self.dimensions
                }
                explicit_roles = [role for role in roles if any(alias and alias in q for alias in role_aliases.get(str(role), []))]
                explicit = len(set(explicit_roles)) == 1
                if len(roles) > 1 and not explicit:
                    prompt = str(case.get("clarification_question") or "Please clarify the role for {value}.").format(value=value)
                    return {"kind": "dimension_role", "value": value, "options": roles, "question": prompt}
            term = case.get("term")
            term_aliases = case.get("terms") or (str(term).split("/") if term else [])
            matched_term = next((str(alias) for alias in term_aliases if str(alias).casefold() in q), None)
            if matched_term:
                options = case.get("candidate_metrics", [])
                if len(options) > 1:
                    # Explicit cues are ontology data, not domain rules in code.
                    explicit_terms = [str(term).casefold() for term in case.get("disambiguating_terms", [])]
                    if not any(term and term in q for term in explicit_terms):
                        prompt = str(case.get("clarification_question") or "Please clarify the metric for {term}.").format(term=matched_term)
                        return {"kind": "metric", "term": matched_term, "options": options, "question": prompt}
        return None
