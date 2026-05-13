# Cisco Org Crawler Architecture Review

Date: 2026-05-13

This review focuses on the local Cisco org crawler/search system in `tools/cisco_org_overlay` and the local data roots under `output/research`. The wider repository is a multi-project workspace; the org crawler is mostly self-contained and should stay that way so sensitive directory data remains local to this Mac.

## Current Shape

The current implementation has four practical layers, but they are only partially separated in code:

- `crawler.py` is the generic overlay crawler, document ingester, Directory API client, normalizer, report writer, and SQLite repository in one large module.
- `focused_crawl.py` generalizes the Jeetu/Oliver deterministic crawls, but still embeds schema, crawl state, report generation, normalization, and fetch behavior together.
- `focused_supervisor.py` manages session refresh, directory access probes, crawler process lifecycle, status reports, and completion criteria.
- `search_databases.py` and `search_frontend.py` provide a read-only local browser/search surface over the generic and focused SQLite databases.

The strongest part of the design is operational safety: the databases are local, the UI binds to `127.0.0.1`, the search path opens SQLite read-only with `query_only`, and the focused crawlers have explicit completion reports.

The biggest architectural drag is coupling. Fetching, normalization, graph persistence, retry policy, status reporting, and root-specific behavior are interleaved, which makes safe changes harder than they need to be.

## Key Findings

- The focused crawl graph source of truth is `org_edges`, not `people.manager_alias`. Current Jeetu and Oliver focused rows have manager aliases largely empty in `people`, while parent-child edges are populated.
- The generic and focused schemas overlap conceptually but diverge physically. Generic uses `person_paths`, labels, documents, and FTS; focused uses `org_edges`, `manager_checks`, and unresolved aliases.
- Root-specific wrappers are lightweight, but root behavior is still encoded in scripts instead of data/config. That is workable for two roots, but less pleasant for a larger root catalog.
- Supervisor logic does the right operational things, but session refresh, crawler restart decisions, and completion semantics would be easier to reason about as separate services.
- Crawl resumption is effective, but a durable frontier table with explicit attempt history would make failures, retries, and auditability cleaner.
- The local UI is useful and safe, but graph operations should move into a shared read model instead of accumulating directly in the HTTP handler.

## Recommended Architecture

Move toward a small package with explicit components:

```text
tools/cisco_org_overlay/
  cisco_org/
    auth.py              # storage-state, extra headers, session probes
    directory_client.py  # profile/direct-report fetches and pagination
    normalize.py         # profile/contact/email/phone normalization
    schema.sql           # one canonical schema with migrations
    repository.py        # SQLite writes, read-only query helpers
    frontier.py          # resumable crawl queue and retry policy
    graph.py             # ancestors, peers, descendants, completion checks
    reports.py           # status, CSV, Markdown, packaged exports
    service.py           # local read API used by CLI and browser UI
  wrappers/
    run_focused.py
    supervise_focused.py
```

The canonical schema should preserve the current strengths while converging generic and focused data:

- `people_current`: one latest row per alias/person.
- `person_snapshots`: immutable captured profile facts.
- `edges_current`: parent-child reporting edges with source, first/last seen, run id, and active flag.
- `crawl_runs`: root, mode, status, timings, session manifest, and notes.
- `crawl_frontier`: alias, root, state, priority, attempts, next eligible time, parent hint.
- `fetch_attempts`: URL/alias, status, latency, error class, body hash, and run id.
- `unresolved_aliases`: durable 404/transient classification.
- `documents` and `document_mentions`: keep as generic overlay enrichment.
- FTS tables or external-content FTS views for people, snapshots, documents, and manager checks.

The crawler loop should become:

1. Load root config from data, not a one-off wrapper.
2. Refresh/probe auth only when the probe says access is stale.
3. Pull eligible aliases from `crawl_frontier`.
4. Fetch profile and direct-report pages through one Directory client.
5. Normalize once into typed profile/direct-report records.
6. Upsert people, snapshots, and `edges_current` in one transaction.
7. Write `fetch_attempts` and update frontier state for retry/completion.
8. Generate reports from read models, not from the crawl loop.

## Migration Plan

- Completed 2026-05-13: Added focused edge metadata support (`active`, `first_seen_at`, `last_seen_at`, `last_run_id`) plus `org_edge_observations` so refreshes can preserve edge history instead of only overwriting a parent-child pair.
- Completed 2026-05-13: Added `--refresh-existing` to the focused crawler/supervisor path so an intentional refresh can refetch known people and manager checks without deleting the DB.
- Completed 2026-05-13: Added `crawler_preflight.py` to verify local session artifacts, process safety, SQLite health, focused graph metadata, stale runs, and UI health before any rerun.
- Phase 1: Extract graph read helpers. Move ancestor/peer/root-chain logic out of `search_frontend.py` into a reusable `graph.py`, keeping existing DBs unchanged.
- Phase 2: Extract Directory client and normalizers from `crawler.py`/`focused_crawl.py`; add unit-style fixture tests that lock profile/email/phone preconditions.
- Phase 3: Add a durable `crawl_frontier` and `fetch_attempts` table to focused DBs while continuing to write the existing tables for compatibility.
- Phase 4: Add a canonical read API used by CLI, browser UI, reports, and package/export scripts.
- Phase 5: Unify Jeetu/Oliver/root wrappers into one config-driven focused crawler and supervisor command.
- Phase 6: Optionally migrate the generic overlay DB into the canonical schema, keeping documents and labels as enrichment tables.

## Org Chart Feature Direction

The new browser chart should use `org_edges` to build:

- Selected person or people.
- Their same-manager peer group.
- The manager chain from that manager up to the configured root.
- Root-aware rendering for Jeetu (`jeetup`) and Oliver (`otuszik`).
- Direct-report drill-down from any rendered node with captured directs, rebuilding the chart around that person and showing direct reports as the next lower level.

Longer term, this belongs in a shared graph read model:

- `get_parent(alias, root)`
- `get_peers(alias, root)`
- `get_ancestor_chain(alias, root)`
- `get_subtree(alias, depth)`
- `get_chart_bundle(selected_aliases, root)`

That gives the UI, Markdown exports, and any future package/export tooling the same graph semantics without duplicating SQL.
