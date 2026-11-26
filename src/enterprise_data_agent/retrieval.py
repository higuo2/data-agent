# 候选检索：先缩到少数系统，再在系统内做词重叠、字段、指标别名和编号的混合召回。
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
        self._tables_by_system: dict[str, list[Table]] = {}
        for table in catalog.tables:
            self._tables_by_system.setdefault(table.system_id, []).append(table)
        self._metrics_by_id = {
            str(m.get("id") or ""): m for m in catalog.ontology.get("metrics", []) if m.get("id")
        }
        self._aliases_by_table: dict[str, str] = {}
        self._aliases_by_system: dict[str, str] = {}
        for metric in catalog.ontology.get("metrics", []):
            blob = " ".join(str(a) for a in metric.get("aliases", []) if a)
            if not blob:
                continue
            for model in self._models_for_metric(metric):
                self._aliases_by_table[model] = (self._aliases_by_table.get(model, "") + " " + blob).strip()
                sid = model.split(".", 1)[0] if "." in model else ""
                if sid:
                    self._aliases_by_system[sid] = (self._aliases_by_system.get(sid, "") + " " + blob).strip()
        for dim in catalog.ontology.get("dimensions", []):
            blob = " ".join(str(a) for a in dim.get("aliases", []) if a)
            field = str(dim.get("field") or "")
            table_ref = field.rpartition(".")[0]
            if blob and table_ref:
                self._aliases_by_table[table_ref] = (self._aliases_by_table.get(table_ref, "") + " " + blob).strip()
                sid = table_ref.split(".", 1)[0]
                self._aliases_by_system[sid] = (self._aliases_by_system.get(sid, "") + " " + blob).strip()

    def _weight(self, name: str, default: float | None = None) -> float:
        if name in self.settings:
            return float(self.settings[name])
        if default is not None:
            return default
        raise KeyError(name)

    def _ngram_sizes(self) -> tuple[int, ...]:
        values = self.settings["character_ngram_sizes"]
        sizes = tuple(int(v) for v in values if int(v) > 0)
        if not sizes:
            raise ValueError("character_ngram_sizes must contain a positive integer")
        return sizes

    def retrieve(self, question: str) -> tuple[list[dict[str, float | str]], list[Candidate]]:
        top_systems = int(self.settings.get("top_systems") or 3)
        top_tables = int(self.settings.get("top_tables") or 8)
        systems = self.rank_systems(question, top_systems)
        allowed = {str(item["system_id"]) for item in systems}
        tables = self.search(question, top_tables, allowed)
        return systems, tables

    def rank_systems(self, question: str, limit: int = 3) -> list[dict[str, float | str]]:
        qtokens = tokens(question)
        q = (question or "").casefold()
        ngram_sizes = self._ngram_sizes()
        qgrams = _char_ngrams(question, ngram_sizes)
        identifier_systems = {ref.split(".", 1)[0] for ref in self._identifier_targets(question) if "." in ref}
        metric_systems = self._metric_system_hits(question)
        token_w = self._weight("token_overlap_weight")
        gram_w = self._weight("character_ngram_weight")
        scored: list[tuple[str, float]] = []
        for system in self.catalog.systems:
            sid = str(system.get("system_id") or "")
            parts = [sid, str(system.get("description") or ""), str(system.get("domain") or ""), self._aliases_by_system.get(sid, "")]
            for table in self._tables_by_system.get(sid, []):
                parts.append(table.name)
                parts.append(table.description)
                if table.domain:
                    parts.append(table.domain)
            text = " ".join(parts)
            ttokens = tokens(text)
            token_overlap = len(qtokens & ttokens) / max(1, len(qtokens))
            gram_overlap = len(qgrams & _char_ngrams(text, ngram_sizes)) / max(1, len(qgrams))
            score = token_w * token_overlap + gram_w * gram_overlap
            if sid.casefold() in q:
                score += self._weight("exact_table_name_boost")
            if sid in identifier_systems:
                score += self._weight("identifier_target_boost")
            if sid in metric_systems:
                score += self._weight("metric_alias_boost")
            scored.append((sid, round(score, 4)))
        ranked = sorted(scored, key=lambda item: (-item[1], item[0]))[:limit]
        return [{"system_id": sid, "score": score} for sid, score in ranked]

    def search(
        self,
        question: str,
        limit: int = 8,
        system_ids: set[str] | None = None,
    ) -> list[Candidate]:
        qtokens = tokens(question)
        q = (question or "").casefold()
        ngram_sizes = self._ngram_sizes()
        qgrams = _char_ngrams(question, ngram_sizes)
        identifier_targets = self._identifier_targets(question)
        scored: list[Candidate] = []
        for table in self.catalog.tables:
            if system_ids is not None and table.system_id not in system_ids:
                continue
            evidence: list[str] = []
            text = " ".join(
                [table.system_id, table.name, table.description, table.grain, table.domain, self._aliases_by_table.get(table.ref, "")]
                + [c.name + " " + c.description for c in table.columns]
            )
            ttokens = tokens(text)
            token_overlap = len(qtokens & ttokens) / max(1, len(qtokens))
            tgrams = _char_ngrams(text, ngram_sizes)
            gram_overlap = len(qgrams & tgrams) / max(1, len(qgrams))
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
                if table.ref not in self._models_for_metric(metric):
                    continue
                aliases = [str(a) for a in metric.get("aliases", [])]
                alias_hit = self._alias_hit(q, qgrams, ngram_sizes, aliases)
                if alias_hit:
                    score += self._weight("metric_alias_boost"); evidence.append(f"metric alias: {alias_hit}")
                metric_text = " ".join(aliases + [str(metric.get("id") or "")])
                if qtokens & tokens(metric_text):
                    score += self._weight("metric_semantic_boost"); evidence.append("metric semantic match")
            alias_grams = _char_ngrams(self._aliases_by_table.get(table.ref, ""), ngram_sizes)
            shared = qgrams & alias_grams
            if alias_grams and self._alias_grams_relevant(shared, alias_grams):
                score += self._weight("metric_semantic_boost")
                evidence.append("ontology alias n-grams")
            if table.deprecated:
                score -= self._weight("deprecated_penalty", 0.0)
                evidence.append("deprecated table penalty")
            if score >= self.min_score:
                scored.append(Candidate(table.ref, round(score, 4), tuple(dict.fromkeys(evidence)), table.system_id, table.name))
        scored = self._cap_per_system(scored)
        return sorted(scored, key=lambda x: (-x.score, x.ref))[:limit]

    def _cap_per_system(self, scored: list[Candidate]) -> list[Candidate]:
        per = int(self.settings.get("per_system_tables") or 0)
        if per <= 0 or not scored:
            return scored
        grouped: dict[str, list[Candidate]] = {}
        for item in sorted(scored, key=lambda x: (-x.score, x.ref)):
            bucket = grouped.setdefault(item.system_id, [])
            if len(bucket) < per:
                bucket.append(item)
        return [item for group in grouped.values() for item in group]

    def _metric_system_hits(self, question: str) -> set[str]:
        q = (question or "").casefold()
        qtokens = tokens(question)
        hits: set[str] = set()
        ngram_sizes = self._ngram_sizes()
        qgrams = _char_ngrams(question, ngram_sizes)
        for metric in self.catalog.ontology.get("metrics", []):
            aliases = [str(a) for a in metric.get("aliases", [])]
            alias_hit = self._alias_hit(q, qgrams, ngram_sizes, aliases)
            metric_text = " ".join(aliases + [str(metric.get("id") or "")])
            if not alias_hit and not (qtokens & tokens(metric_text)):
                continue
            hits.update({model.split(".", 1)[0] for model in self._models_for_metric(metric) if "." in model})
        return hits

    def _alias_grams_relevant(self, shared: set[str], alias_grams: set[str]) -> bool:
        if not shared:
            return False
        if any(any("\u4e00" <= ch <= "\u9fff" for ch in gram) for gram in shared):
            return True
        return len(shared) / max(1, len(alias_grams)) >= self._weight("min_metric_ngram_overlap", 0.5)

    def _alias_hit(self, question: str, qgrams: set[str], ngram_sizes: tuple[int, ...], aliases: list[str]) -> str | None:
        threshold = self._weight("min_metric_ngram_overlap", 0.5)
        min_len = int(self.settings.get("min_alias_length") or 2)
        for alias in aliases:
            if not alias:
                continue
            folded = alias.casefold()
            if folded in question:
                return alias
            if len(folded) >= min_len and _is_subsequence(folded, question):
                return alias
            grams = _char_ngrams(alias, ngram_sizes)
            if grams and len(qgrams & grams) / len(grams) >= threshold:
                return alias
        return None

    def _models_for_metric(self, metric: dict) -> set[str]:
        model = str(metric.get("model") or "")
        if model:
            return {model}
        spec = metric.get("composition") or {}
        found: set[str] = set()
        for side in ("minuend", "subtrahend"):
            child = self._metrics_by_id.get(str(spec.get(side) or ""))
            if child:
                found.update(self._models_for_metric(child))
        return found

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


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(char in iterator for char in needle)
