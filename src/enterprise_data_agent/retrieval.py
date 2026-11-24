# 候选检索：用词重叠、中文字片段、表名、字段、指标别名和编号模式给表和系统打分。
from __future__ import annotations
import re
from dataclasses import dataclass
from .catalog import Catalog, Table

_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def tokens(text: str) -> set[str]:
    return {x.casefold() for x in _TOKEN.findall(text or "") if len(x) > 1}


def _char_ngrams(text: str, sizes: tuple[int, ...]) -> set[str]:
    """Character n-grams provide overlap evidence for unsegmented CJK text."""
    compact = "".join(ch.casefold() for ch in (text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {compact[i:i + size] for size in sizes if len(compact) >= size for i in range(len(compact) - size + 1)}


@dataclass(frozen=True)
class Candidate:
    ref: str
    score: float
    reason: tuple[str, ...]
    system_id: str
    table_name: str


class SemanticRetriever:
    def __init__(self, catalog: Catalog, settings: dict):
        self.catalog = catalog
        self.min_score = float(settings["minimum_lexical_score"])
        self.settings = settings

    def _weight(self, name: str) -> float:
        return float(self.settings[name])

    def _ngram_sizes(self) -> tuple[int, ...]:
        values = self.settings["character_ngram_sizes"]
        sizes = tuple(int(v) for v in values if int(v) > 0)
        if not sizes:
            raise ValueError("character_ngram_sizes must contain a positive integer")
        return sizes

    def search(self, question: str, limit: int = 8) -> list[Candidate]:
        qtokens = tokens(question)
        q = (question or "").casefold()
        ngram_sizes = self._ngram_sizes()
        qgrams = _char_ngrams(question, ngram_sizes)
        identifier_targets = self._identifier_targets(question)
        scored: list[Candidate] = []
        for table in self.catalog.tables:
            evidence: list[str] = []
            text = " ".join([table.system_id, table.name, table.description, table.grain] + [c.name + " " + c.description for c in table.columns])
            ttokens = tokens(text)
            token_overlap = len(qtokens & ttokens) / max(1, len(qtokens))
            tgrams = _char_ngrams(text, ngram_sizes)
            gram_overlap = len(qgrams & tgrams) / max(1, len(qgrams))
            # Keep scores bounded and avoid rewarding long metadata descriptions
            # merely for containing many distinct tokens/ngrams.
            overlap = self._weight("token_overlap_weight") * token_overlap + self._weight("character_ngram_weight") * gram_overlap
            score = overlap
            if table.name.casefold() in q:
                score += self._weight("exact_table_name_boost"); evidence.append("exact table name")
            field_hits = [c.name for c in table.columns if c.name.casefold() in q and len(c.name) >= 4]
            if field_hits:
                score += min(self._weight("field_match_cap"), self._weight("field_match_boost") * len(field_hits)); evidence.append("field tokens: " + ", ".join(field_hits[:4]))
            if token_overlap:
                evidence.append(f"metadata token overlap {token_overlap:.2f}")
            if gram_overlap:
                evidence.append(f"metadata character n-gram overlap {gram_overlap:.2f}")
            if table.ref in identifier_targets:
                score += self._weight("identifier_target_boost")
                evidence.append("identifier pattern target")
            for metric in self.catalog.ontology.get("metrics", []):
                if table.ref == metric.get("model"):
                    aliases = [str(a) for a in metric.get("aliases", [])]
                    alias_hit = next((a for a in aliases if a.casefold() in q), None)
                    if alias_hit:
                        score += self._weight("metric_alias_boost"); evidence.append(f"metric alias: {alias_hit}")
                    metric_text = " ".join(aliases + [str(metric.get("id") or "")])
                    if qtokens & tokens(metric_text):
                        score += self._weight("metric_semantic_boost"); evidence.append("metric semantic match")
            if score >= self.min_score:
                scored.append(Candidate(table.ref, round(score, 4), tuple(dict.fromkeys(evidence)), table.system_id, table.name))
        return sorted(scored, key=lambda x: (-x.score, x.ref))[:limit]

    def _identifier_targets(self, question: str) -> set[str]:
        targets: set[str] = set()
        for item in self.catalog.ontology.get("identifier_patterns", []):
            try:
                found = re.search(str(item["regex"]), question or "", re.I)
            except (KeyError, re.error):
                continue
            if found and item.get("table"):
                target = str(item["table"])
                if "." not in target:
                    target = f"{item.get('system_id', '')}.{target}".strip(".")
                targets.add(target)
        return targets

    def ranked_systems(self, candidates: list[Candidate], limit: int = 3) -> list[dict[str, float | str]]:
        totals: dict[str, float] = {}
        for c in candidates:
            totals[c.system_id] = max(totals.get(c.system_id, 0.0), c.score)
        return [{"system_id": sid, "score": round(score, 4)} for sid, score in sorted(totals.items(), key=lambda p: (-p[1], p[0]))[:limit]]
