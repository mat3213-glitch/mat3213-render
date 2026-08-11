# Repo Scout current-needs migration

`repo_scout_current_needs.v1.json` replaces `repo_scout_queries.json` as the source of search intent.
The legacy query file remains only for rollback/reference and is no longer read by `repo_scout.py`.

## v1 rules

- A candidate must contain at least one configured positive evidence term in repository metadata.
- Global or per-need negative terms reject it before shortlist and LLM enrichment.
- Stars and trending velocity only break ties after need priority and evidence coverage.
- `latest.md` is a view. Lifecycle decisions remain in `repo_scout_ledger.json`.
- Grok GitHub links use the same current-needs and lifecycle gates before analyst queueing.

## Auto Analyst and Adoption Board

`repo_scout_ledger.json` is also the sole lifecycle source of truth for Auto Analyst. Explicit
GitHub targets with `adopted`, `rejected`, `park` or `pilot` are excluded before the matrix.
`ADOPTION_BOARD.md` is rebuilt as a view from `report.json` plus the ledger; never edit statuses
in the Markdown board. Move any historical manual decision into the ledger before rebuilding.

GitHub targets must resolve to a real `owner/repo`; root, search, topics, org and malformed pages
are discarded. External non-GitHub HTTP(S) tools and articles remain valid analyst targets and
remain `PENDING` until a separate lifecycle mechanism is introduced for them.

## Updating needs

Create a new versioned config for a schema-breaking change. For a routine gap update, edit v1's
`priority`, `gap`, `queries`, positive evidence, negative terms and duplicate-risk explanation together.
Do not add a query unless its results can satisfy explicit positive evidence. Keep saturated or forbidden
directions in `global_negative_terms`; do not rely on an LLM to reject them later.
