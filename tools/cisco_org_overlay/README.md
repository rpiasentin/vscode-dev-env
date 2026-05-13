# Cisco Org Overlay

Local-only crawler and archive for Cisco org overlays.

## What It Does

- Crawls Cisco Directory from one or more roots.
- Stores a time-series history in SQLite.
- Preserves path membership, so we can ask questions like "under Oliver" or "within Chuck 6 layers".
- Stores searchable labels for likely sales, sales engineering, partner sales, account ownership, and AI domains.
- Keeps document metadata, extracted text, and document-to-person mention links for later PPT/PDF enrichment.

## Local Storage

- Database: [`output/research/cisco-org-overlay/cisco_org_overlay.sqlite3`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/cisco_org_overlay.sqlite3)
- Reports: [`output/research/cisco-org-overlay/reports`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/reports)
- Archive: [`output/research/cisco-org-overlay/archive`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/archive)
- Extracts: [`output/research/cisco-org-overlay/extracts`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/extracts)
- Worklog: [`output/research/cisco-org-overlay/worklog.md`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/worklog.md)

Everything is intended to stay on this machine.

## First-Run Flow

1. Export authenticated browser state from the Cisco internal session into:
   [`output/research/cisco-org-overlay/storage-state.json`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/storage-state.json)
   If the cookie-only session export is not enough for Directory API reuse, capture the live browser headers too:

```bash
cd tools/cisco_org_overlay
npm install
npm run hardened-session
```

This also writes:

- [`output/research/cisco-org-overlay/directory-extra-headers.json`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/directory-extra-headers.json)
- [`output/research/cisco-org-overlay/session-manifest.json`](/Users/rpias/dev/vscode-dev-env/output/research/cisco-org-overlay/session-manifest.json)

2. Initialize the local database:

```bash
python3 tools/cisco_org_overlay/crawler.py init
```

3. Run the first directory crawl:

```bash
python3 tools/cisco_org_overlay/crawler.py crawl-directory \
  --extra-headers output/research/cisco-org-overlay/directory-extra-headers.json \
  --root crobbins:6 \
  --root otuszik:leaf
```

4. Seed or ingest discovered documents later:

```bash
python3 tools/cisco_org_overlay/crawler.py ingest-document-manifest \
  --manifest output/research/cisco-org-overlay/seed-documents.json
```

## Output Shape

Each successful crawl writes:

- `people-latest-run-<id>.csv`
- `paths-run-<id>.csv`
- `crawl-summary-run-<id>.json`
- `changes-run-<id>.md`

## Ad Hoc Search

Use the local read-only search helper to run free-text searches without hand-writing SQL:

```bash
python3 tools/cisco_org_overlay/search_databases.py --list-dbs
python3 tools/cisco_org_overlay/search_databases.py --check --db jeetu
python3 tools/cisco_org_overlay/search_databases.py "Jeetu Patel" --db jeetu
python3 tools/cisco_org_overlay/search_databases.py icastrov --db oliver --table manager_checks
python3 tools/cisco_org_overlay/search_databases.py "org chart" --db generic --table documents
python3 tools/cisco_org_overlay/search_databases.py "8145 3000" --db jeetu --table snapshots --deep --json
```

Behavior notes:

- The script opens the databases read-only and sets SQLite `query_only`.
- By default it searches current-state records and status tables.
- Use `--table snapshots` for historical snapshot rows.
- Use `--deep` to include raw JSON and metadata blobs in the search text.
- Known database aliases are `generic`, `jeetu`, `oliver`, and `all`.

## Search UI

For a local browser UI on top of the same search engine:

```bash
python3 tools/cisco_org_overlay/search_frontend.py
```

Then open the printed `http://127.0.0.1:8765` URL manually in a browser on this Mac.

UI notes:

- The server binds to `127.0.0.1` by default and does not open a browser window automatically.
- If `8765` is already busy, the launcher will automatically move to the next available local port and print the new URL.
- The health panel checks each known database before you search.
- The results pane can export the current search result set as a local `.md` document.
- Search requests still run through the same read-only SQLite path used by the CLI helper.

## Preflight Before Focused Reruns

Run the local preflight before starting any Jeetu or Oliver refresh:

```bash
python3 tools/cisco_org_overlay/crawler_preflight.py --db jeetu --db oliver --check-ui
```

Use `--require-focused-v2` to make missing focused graph metadata a hard failure. Use `--apply-focused-migrations` only when you are ready to apply the additive focused schema migration locally before a refresh.

When a refresh is explicitly approved, prefer the non-destructive focused mode that refetches known people and direct-report checks while preserving the existing DB:

```bash
python3 tools/cisco_org_overlay/jeetu_supervisor.py --refresh-existing --status-interval 300
```

Do not run the Oliver supervisor unless the Oliver refresh has been explicitly requested.

## Notes

- `Directory` is the default current-state source of truth.
- Documents are stored as supporting evidence and enrichment.
- The schema is time-series friendly so daily refreshes can produce change reports later.
