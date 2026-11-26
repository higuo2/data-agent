# Data Agent Development Rules

These instructions govern implementation in this repository.
The project is a clean-room enterprise data-agent rewrite. Do not modify the
original `umodel-agent-platform` repository unless the user redirects the work.

## Delivery workflow

- Work in numbered phases. Finish the current phase's stated scope, automated
  checks, documentation, and `progress.md` update before reporting completion.
- After each phase, stop and report the outcome and evidence. Do not start the
  next phase until the user has reviewed the checkpoint and asked to continue.
- When new edge cases or requirements appear, incorporate them into the active
  phase's tests/docs if they are within scope; record deferred work explicitly.
- Ask the user promptly when a business definition, data authority, security
  policy, or conflicting source-of-truth cannot be resolved from the catalog.

## Supervisor acceptance requirements

The finished system must address all of the following; a demo that only answers
fixed question templates is not sufficient:

1. Classify data questions, smalltalk, external-tool requests (such as weather),
   unsafe mutations, unsupported requests, and requests needing clarification.
2. For data requests, discover the appropriate business system, database, and
   a small set of relevant tables from metadata. The target environment is dozens
   of systems and hundreds to thousands of tables, not a hand-picked demo schema.
3. Align business entities and metrics across schemas using UModel and NVIDIA
   Auto Ontology as the first semantic foundation. Reduce manual synonym lists
   through metadata profiling, ontology/entity extraction, candidate ranking,
   relationship evidence, and user clarification. Measure retrieval quality.
   Consider Wren AI/dbt only if evaluation identifies a concrete capability gap.
4. Resolve identifiers such as `SK-0001` to a catalog-declared entity key and
   verify exact values against the selected source using safe, read-only access.
5. Detect collisions such as buyer city versus seller city. Never silently choose
   a role when evidence is insufficient; return a typed clarification request and
   preserve the answer in conversation state when multi-turn handling is added.
6. Produce correct aggregates with explicit metric definitions, grain, time and
   currency semantics, join cardinality checks, and fan-out protection.
7. Provide a real Agent workflow: registered typed tools, bounded planning and
   observation, context/memory management, result post-processing, termination
   checks, and bounded retry/recovery. Do not claim these capabilities before
   they exist and have tests/evaluation.
8. Include enterprise controls before production claims: identity propagation,
   authorization/row and column policies, read-only execution, audit, budgets,
   cancellation, secret handling, and schema-drift handling.

## Configuration and hardcoding policy

- Do not embed business-specific system/table/column names, identifiers, metric
  definitions, aliases, routing vocabulary, ambiguity policy, thresholds, ranking
  weights, SQL limits, retry limits, or prompts as scattered code constants.
  Put changeable policy in validated configuration or versioned catalog/ontology
  data and document its owner and reload behavior.
- Keep algorithms and invariant enforcement in code; keep business vocabulary,
  model mappings, policy values, and tuning parameters in data/config. Avoid
  disguising a hardcoded rule as a generic helper.
- Configuration must be schema-validated, have safe bounds/default behavior,
  and fail clearly for invalid or conflicting definitions. Do not silently
  accept malformed patterns or unsafe SQL metadata.
- Add tests proving that configuration changes affect behavior without source
  edits. Preserve deterministic evidence/reasons for routing and retrieval.

## Safety and query correctness

- Treat user text, model output, catalog metadata, and entity values as untrusted.
- Use parameter binding for values, identifier allowlists for SQL identifiers,
  least privilege, and read-only connections. Never execute model-authored SQL
  directly.
- Before aggregation, validate metric authority, grain, relationship direction
  and cardinality, time/currency requirements, access scope, and fan-out risk.
- Reject unsupported or ambiguous plans with structured reasons. Never fabricate
  an answer from missing data or imply a candidate ranking score is confidence.
- Keep the compiler bounded to proven plan shapes. The Phase 2 contract remains
  one fact plus N catalog-approved N:1 (or role-filtered 1:N) dimensions, with
  no model-authored SQL. Phase 5 additionally allows only: (1) a 1:N join whose
  ontology `current_filter` restores one row per fact key, with a uniqueness
  probe that still rejects duplicate current rows; (2) a composite metric that
  subtracts two certified same-grouping aggregates run as separate read-only
  plans, not nested SQL; (3) an equality `as_of_date` filter on a snapshot fact
  table; (4) canonical-entity alignment through a validity-window mapping table
  as a second read-only query plus in-memory merge, never a cross-source JOIN.
  Do not build arbitrary nested SQL or free CTEs as a shortcut.

## Validation and progress record

- Add regression tests for every fixed defect and acceptance behavior. Use the
  synthetic SQLite fixtures for exact lookup and execution safety; add evaluation
  cases for many-table recall and ambiguity before claiming generality.
- Do not call a phase complete based on code presence alone. Record commands and
  outcomes, known limitations, and any external integrations not exercised in
  `progress.md`.
- `progress.md` is the only status record. Put goals, completed work, and
  remaining work there. Do not add parallel progress, handoff, or design-status
  documents. Keep the README limited to how to run the project.
