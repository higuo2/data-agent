# 槽位：只提出指标、维度、时间和过滤值。不能带 SQL、连接或字段引用。
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any
from .retrieval import _char_ngrams

_ANSWER_SPLIT = re.compile(r"[\s,，。；;、]+")

_FORBIDDEN_SLOT_KEYS = frozenset({
    "joins", "sql", "field_id", "field_ids", "query_mode", "metrics", "filters",
})
_ALIAS_MODES = frozenset({"exact", "subsequence"})


@dataclass
class SlotProposal:
    metric_ids: tuple[str, ...] = ()
    dimension_ids: tuple[str, ...] = ()
    time_phrases: tuple[str, ...] = ()
    currency_codes: tuple[str, ...] = ()
    extracted_constraints: tuple[str, ...] = ()
    unmatched_constraints: tuple[str, ...] = ()
    source: str = "lexical"
    time_bucket_grain: str | None = None
    comparisons: tuple[dict[str, Any], ...] = ()
    in_values: tuple[str, ...] = ()
    top_n: int | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "SlotProposal":
        extra = _FORBIDDEN_SLOT_KEYS.intersection(raw)
        if extra:
            raise ValueError(f"slot proposal cannot include {sorted(extra)}")
        comparisons = _comparison_tuple(raw.get("comparisons"))
        top_n = raw.get("top_n")
        if top_n is not None:
            top_n = int(top_n)
            if top_n < 1:
                raise ValueError("top_n must be a positive integer")
        grain = raw.get("time_bucket_grain")
        return cls(
            metric_ids=_string_tuple(raw.get("metric_ids")),
            dimension_ids=_string_tuple(raw.get("dimension_ids")),
            time_phrases=_string_tuple(raw.get("time_phrases")),
            currency_codes=_string_tuple(raw.get("currency_codes")),
            extracted_constraints=_string_tuple(raw.get("extracted_constraints")),
            unmatched_constraints=_string_tuple(raw.get("unmatched_constraints")),
            source=str(raw.get("source") or "injected"),
            time_bucket_grain=str(grain) if grain else None,
            comparisons=comparisons,
            in_values=_string_tuple(raw.get("in_values")),
            top_n=top_n,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "metric_ids": list(self.metric_ids),
            "dimension_ids": list(self.dimension_ids),
            "time_phrases": list(self.time_phrases),
            "currency_codes": list(self.currency_codes),
            "extracted_constraints": list(self.extracted_constraints),
            "unmatched_constraints": list(self.unmatched_constraints),
            "source": self.source,
            "time_bucket_grain": self.time_bucket_grain,
            "comparisons": [dict(item) for item in self.comparisons],
            "in_values": list(self.in_values),
            "top_n": self.top_n,
        }


@dataclass
class SessionState:
    original_question: str = ""
    answers: list[str] = field(default_factory=list)
    pending_clarification: dict[str, Any] | None = None
    resolved_choices: dict[str, str] = field(default_factory=dict)
    partial_plan: dict[str, Any] | None = None
    closed: bool = False

    def working_question(self) -> str:
        parts = [self.original_question, *self.answers]
        return " ".join(part for part in parts if part).strip()

    def to_json(self) -> dict[str, Any]:
        return {
            "original_question": self.original_question,
            "answers": list(self.answers),
            "pending_clarification": dict(self.pending_clarification) if self.pending_clarification else None,
            "resolved_choices": dict(self.resolved_choices),
            "partial_plan": dict(self.partial_plan) if self.partial_plan else None,
            "closed": self.closed,
        }


def extract_slots(question: str, ontology: dict[str, Any], config: dict[str, Any]) -> SlotProposal:
    cfg = _slot_config(config)
    q = (question or "").casefold()
    metrics = _ranked_ids(q, ontology.get("metrics", []), cfg)
    if not metrics and cfg["alias_match"] == "subsequence":
        metrics = _ngram_ranked_ids(q, ontology.get("metrics", []), cfg)
    dimensions = _all_ids(q, ontology.get("dimensions", []), cfg)
    time_phrases = tuple(
        str(item.get("phrase") or "")
        for item in config.get("planner", {}).get("time_phrases", [])
        if str(item.get("phrase") or "") and str(item.get("phrase")).casefold() in q
    )
    currency_codes = tuple(dict.fromkeys(
        str(item.get("code") or "")
        for item in config.get("planner", {}).get("currency_mentions", [])
        if str(item.get("phrase") or "").casefold() in q and item.get("code")
    ))
    grain, grain_conflict = _time_bucket_grain(q, config)
    comparisons = _comparisons(question or "", config)
    top_n, top_conflict = _top_n(question or "", config)
    in_values = _in_values(question or "", ontology, config, metrics, dimensions)
    extracted = list(time_phrases)
    extracted.extend(currency_codes)
    extracted.extend(metrics)
    extracted.extend(dimensions)
    if grain:
        extracted.append(f"time_bucket:{grain}")
    if comparisons:
        extracted.append("comparison")
    if in_values:
        extracted.append("in_values")
    if top_n:
        extracted.append(f"top_n:{top_n}")
    unmatched = []
    if grain_conflict:
        unmatched.append("time_bucket")
    if top_conflict:
        unmatched.append("top_n")
    for signal in cfg["unsupported_constraint_signals"]:
        phrases = [str(p) for p in signal.get("phrases") or [] if p]
        if any(phrase.casefold() in q for phrase in phrases):
            unmatched.append(str(signal.get("id") or "unsupported_constraint"))
            extracted.append(str(signal.get("id") or "unsupported_constraint"))
    return SlotProposal(
        metric_ids=metrics,
        dimension_ids=dimensions,
        time_phrases=time_phrases,
        currency_codes=currency_codes,
        extracted_constraints=tuple(dict.fromkeys(extracted)),
        unmatched_constraints=tuple(dict.fromkeys(unmatched)),
        source="lexical",
        time_bucket_grain=grain,
        comparisons=comparisons,
        in_values=in_values,
        top_n=top_n,
    )


def merge_clarification_answer(
    answer: str,
    clarification: dict[str, Any],
    ontology: dict[str, Any],
) -> str | None:
    options = [str(item) for item in clarification.get("options") or []]
    if not options:
        return None
    text = (answer or "").strip().casefold()
    if not text:
        return None
    exact = [item for item in options if item.casefold() == text]
    if len(exact) == 1:
        return exact[0]
    dims = {str(d.get("id") or ""): d for d in ontology.get("dimensions", [])}
    metrics = {str(m.get("id") or ""): m for m in ontology.get("metrics", [])}
    parts = [part for part in _ANSWER_SPLIT.split(text) if part]
    hits: list[str] = []
    for option in options:
        labels = [option]
        record = dims.get(option) or metrics.get(option) or {}
        labels.extend(str(a) for a in record.get("aliases", []) if a)
        folded_labels = [label.casefold() for label in labels if label]
        if any(_label_hit(text, label) or any(_label_hit(part, label) for part in parts) for label in folded_labels):
            hits.append(option)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return None


def _label_hit(text: str, label: str) -> bool:
    return bool(text) and (text == label or text in label or label in text)


def is_abandon(answer: str, config: dict[str, Any]) -> bool:
    phrases = {str(p).casefold() for p in _slot_config(config)["abandon_phrases"] if p}
    return (answer or "").strip().casefold() in phrases


def clarification_kind_key(clarification: dict[str, Any]) -> str:
    return str(clarification.get("kind") or "slot")


def _slot_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = dict(config.get("slot_filling") or {})
    mode = str(raw.get("alias_match") or "subsequence").casefold()
    if mode not in _ALIAS_MODES:
        raise ValueError(f"slot_filling.alias_match must be one of {sorted(_ALIAS_MODES)}")
    min_len = int(raw.get("min_alias_length") or 2)
    if min_len < 1:
        raise ValueError("slot_filling.min_alias_length must be >= 1")
    signals = raw.get("unsupported_constraint_signals") or []
    if not isinstance(signals, list):
        raise ValueError("slot_filling.unsupported_constraint_signals must be a list")
    abandon = raw.get("abandon_phrases") or []
    if not isinstance(abandon, list):
        raise ValueError("slot_filling.abandon_phrases must be a list")
    connectors = raw.get("value_connectors") or ["和", "与", "、"]
    if not isinstance(connectors, list):
        raise ValueError("slot_filling.value_connectors must be a list")
    strip_tokens = raw.get("strip_tokens") or []
    if not isinstance(strip_tokens, list):
        raise ValueError("slot_filling.strip_tokens must be a list")
    ngram_sizes = tuple(int(v) for v in (raw.get("metric_ngram_sizes") or []))
    if any(size < 1 for size in ngram_sizes):
        raise ValueError("slot_filling.metric_ngram_sizes must contain positive integers")
    overlap = raw.get("min_metric_ngram_overlap")
    overlap = 1.0 if overlap is None else float(overlap)
    if overlap <= 0 or overlap > 1:
        raise ValueError("slot_filling.min_metric_ngram_overlap must be in (0, 1]")
    return {
        "alias_match": mode,
        "min_alias_length": min_len,
        "unsupported_constraint_signals": signals,
        "abandon_phrases": abandon,
        "value_connectors": connectors,
        "strip_tokens": strip_tokens,
        "metric_ngram_sizes": ngram_sizes,
        "min_metric_ngram_overlap": overlap,
    }


def _ranked_ids(question: str, records: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, ...]:
    exact, fuzzy = _partition_hits(question, records, cfg)
    chosen = exact or fuzzy
    return tuple(chosen[:1]) if len(chosen) == 1 else ()


def _ngram_ranked_ids(question: str, records: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, ...]:
    sizes = cfg["metric_ngram_sizes"]
    if not sizes:
        return ()
    qgrams = _char_ngrams(question, sizes)
    if not qgrams:
        return ()
    threshold = cfg["min_metric_ngram_overlap"]
    best_ids: list[str] = []
    best_score = 0.0
    for record in records:
        ident = str(record.get("id") or "")
        if not ident:
            continue
        scores = []
        for alias in record.get("aliases", []) or []:
            grams = _char_ngrams(str(alias), sizes)
            if grams:
                scores.append(len(grams & qgrams) / len(grams))
        score = max(scores) if scores else 0.0
        if score < threshold:
            continue
        if score > best_score:
            best_ids = [ident]
            best_score = score
        elif score == best_score and ident not in best_ids:
            best_ids.append(ident)
    return tuple(best_ids[:1]) if len(best_ids) == 1 else ()


def _all_ids(question: str, records: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, ...]:
    exact, fuzzy = _partition_hits(question, records, cfg)
    return tuple(exact or fuzzy)


def _partition_hits(question: str, records: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    exact: list[str] = []
    fuzzy: list[str] = []
    for record in records:
        ident = str(record.get("id") or "")
        if not ident:
            continue
        aliases = [str(a) for a in record.get("aliases", []) if a]
        kind = _best_alias_hit(question, aliases, cfg)
        if kind == "exact":
            exact.append(ident)
        elif kind == "subsequence":
            fuzzy.append(ident)
    return exact, fuzzy


def _best_alias_hit(question: str, aliases: list[str], cfg: dict[str, Any]) -> str | None:
    found = None
    for alias in aliases:
        folded = alias.casefold()
        if not folded:
            continue
        if folded in question:
            return "exact"
        if (
            cfg["alias_match"] == "subsequence"
            and len(folded) >= cfg["min_alias_length"]
            and _is_subsequence(folded, question)
        ):
            found = "subsequence"
    return found


def _is_subsequence(needle: str, haystack: str) -> bool:
    iterator = iter(haystack)
    return all(char in iterator for char in needle)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if not isinstance(value, list):
        raise ValueError("slot list fields must be arrays of strings")
    items = [str(item).strip() for item in value]
    if any(not item for item in items):
        raise ValueError("slot list fields cannot contain empty strings")
    return tuple(items)


def _comparison_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    if not isinstance(value, list):
        raise ValueError("comparisons must be an array")
    items = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each comparison must be an object")
        if "field_id" in item:
            raise ValueError("slot proposal cannot include field_id")
        op = str(item.get("op") or "").casefold()
        if not op:
            raise ValueError("comparison requires op")
        items.append({"op": op, "value": item.get("value")})
    return tuple(items)


def _time_bucket_grain(question: str, config: dict[str, Any]) -> tuple[str | None, bool]:
    grains = []
    for item in config.get("planner", {}).get("time_bucket_phrases", []):
        phrase = str(item.get("phrase") or "")
        grain = str(item.get("grain") or "").casefold()
        if phrase and grain and phrase.casefold() in question:
            grains.append(grain)
    unique = list(dict.fromkeys(grains))
    if len(unique) == 1:
        return unique[0], False
    if len(unique) > 1:
        return None, True
    return None, False


def _comparisons(question: str, config: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    patterns = sorted(
        list(config.get("planner", {}).get("comparison_patterns") or []),
        key=lambda item: len(str(item.get("phrase") or "")),
        reverse=True,
    )
    used: list[tuple[int, int]] = []
    found: list[dict[str, Any]] = []
    for item in patterns:
        phrase = str(item.get("phrase") or "")
        op = str(item.get("op") or "").casefold()
        if not phrase or not op:
            continue
        for match in re.finditer(re.escape(phrase) + r"\s*(\d+(?:\.\d+)?)", question):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in used):
                continue
            used.append(span)
            raw = match.group(1)
            value: int | float = int(raw) if re.fullmatch(r"\d+", raw) else float(raw)
            found.append({"op": op, "value": value})
    return tuple(found)


def _top_n(question: str, config: dict[str, Any]) -> tuple[int | None, bool]:
    regex_ns: list[int] = []
    phrase_ns: list[int] = []
    for item in config.get("planner", {}).get("top_n_patterns") or []:
        regex = item.get("regex")
        if regex:
            match = re.search(str(regex), question)
            if match:
                regex_ns.append(int(match.group(1)))
            continue
        phrase = str(item.get("phrase") or "")
        if phrase and phrase.casefold() in question.casefold() and item.get("n") is not None:
            phrase_ns.append(int(item["n"]))
    ns = regex_ns or phrase_ns
    unique = list(dict.fromkeys(ns))
    if len(unique) == 1:
        return unique[0], False
    if len(unique) > 1:
        return None, True
    return None, False


def _in_values(
    question: str,
    ontology: dict[str, Any],
    config: dict[str, Any],
    metric_ids: tuple[str, ...],
    dimension_ids: tuple[str, ...],
) -> tuple[str, ...]:
    cfg = _slot_config(config)
    time_ids = {
        str(dim.get("id") or "")
        for dim in ontology.get("dimensions", [])
        if dim.get("time_role")
    }
    role_dims = [ident for ident in dimension_ids if ident not in time_ids]
    if len(role_dims) != 1:
        return ()
    values = []
    for case in ontology.get("ambiguity_cases", []):
        value = str(case.get("value") or "").strip()
        if value and value.casefold() in question.casefold():
            values.append(value)
    leftover = question
    removals: list[str] = [str(t) for t in cfg["strip_tokens"] if t]
    records = {str(item.get("id") or ""): item for item in ontology.get("metrics", [])}
    records.update({str(item.get("id") or ""): item for item in ontology.get("dimensions", [])})
    for ident in (*metric_ids, *dimension_ids):
        removals.extend(str(a) for a in (records.get(ident) or {}).get("aliases", []) if a)
    for item in config.get("planner", {}).get("time_phrases", []):
        removals.append(str(item.get("phrase") or ""))
    for item in config.get("planner", {}).get("currency_mentions", []):
        removals.append(str(item.get("phrase") or ""))
    for item in config.get("planner", {}).get("time_bucket_phrases", []):
        removals.append(str(item.get("phrase") or ""))
    for item in config.get("planner", {}).get("comparison_patterns", []):
        removals.append(str(item.get("phrase") or ""))
    removals = [item for item in removals if item]
    removals.sort(key=len, reverse=True)
    for token in removals:
        leftover = re.sub(re.escape(token), " ", leftover, flags=re.I)
    if not any(connector in question for connector in cfg["value_connectors"]):
        return tuple(values)
    leftover = re.sub(r"\d+(?:\.\d+)?", " ", leftover)
    split_pat = "|".join(re.escape(c) for c in cfg["value_connectors"] + [" ", ",", "，"])
    parts = [part.strip() for part in re.split(split_pat, leftover) if part.strip()]
    for part in parts:
        if len(part) < 2 or part in values:
            continue
        if any(
            alias.casefold().startswith(part.casefold()) or part.casefold() in alias.casefold()
            for alias in removals if alias
        ):
            continue
        values.append(part)
    return tuple(values)
