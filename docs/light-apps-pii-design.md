# MiniApp PII and public-link visibility

This document describes the platform-level controls that keep sensitive data
inside a MiniApp from leaking through agent tool output, viewer reads, CSV
exports, or public read-only links.

## Threat model

- A MiniApp stores user data in control-plane tables; the same data can be read
  by the dashboard, the app assistant, the agent tools, CSV exports, backups,
  and a bearer-token public link.
- Public links have no identity. Treat everything reachable from `/p/{token}`
  as published data.
- A model can put a phone number, ID number, bank card, address, email, or API
  key into a free-text field even when the column is not marked as sensitive.

## The two layers

1. **Column classification** — `Column.pii` is the authoritative policy. Values
   in a marked column are masked wholesale on viewer/public reads.
   Sensitive column names/labels (`phone`, `联系人`, `证件后四位`, `id_tail`,
   `email`, ...) are auto-classified at schema creation so a missing `pii` flag
   does not silently disable protection.
2. **Value detection** — `octop.infra.apps.pii` reuses the harness PII detector
   (mainland mobile numbers, resident IDs, provider keys) and adds bank-card
   (Luhn), email, and expanded Chinese/English header patterns. On viewer/public
   reads, every unmarked string cell is scanned and matching spans are masked
   (for example `李静 13800000002` → `李静 138****0002`). This catches free-text
   PII that column-level metadata missed.

The harness `PIIMiddleware` remains the last line for the model surface: it
masks the input/output/tool-result text the model sees. That masking is
display-only; it does not mean the stored value is encrypted or erased.

## Write path

- `app_create` / `app_add_table` / `app_update_schema` auto-classify sensitive
  headers.
- `app_append_row` / `app_update_row` scan every unmarked string/text value. A
  detected phone/ID/bank/email/secret is rejected with `APP_PII_UNMARKED`; the
  caller must mark the column with `app_alter_schema(pii={...})` or move the
  value to a marked column.
- Imports still use `include_pii=false` by default; header/value detection marks
  sensitive columns and reports them in `pii_columns` / `blocked_pii`.
- `app_alter_schema` accepts `pii` for existing columns (and the dashboard
  `PATCH /api/apps/{app_id}/schema?table=...` body does too), so legacy apps can
  be repaired without dropping data.
- `app_append_row` / `app_update_row` return row id, version, and PII metadata
  instead of echoing the full row.

## Read path

Every read surface goes through `_apply_pii` / `redact_rows`:

- public link rows (`redact_pii=True`, plus table visibility),
- dashboard viewer reads,
- CSV exports for viewers,
- app assistant context,
- `app_read_data` for non-editors.

Marked columns are masked wholesale; unmarked free text is redacted span by
span. Owners/editors still see full values (and are audited).

## Public link scope

`light_app_tables.public_visible` (schema v30) controls which tables are exposed:

- `main` is public by default; add-on tables (`members`, `loans`, ...) are
  private by default.
- `GET /api/public/apps/{token}` lists only public tables.
- `GET /api/public/apps/{token}/rows?table=...` returns 404 for a private table.
- The public app SDK's `tables()` method returns only public tables; generated
  HTML must render tabs from `octopApp.tables()` instead of hardcoding keys.
- `app_set_table_visibility` / `PATCH /api/apps/{app_id}/tables/{table}` toggles
  a table; the dashboard exposes the same switch.
- `app_share_link` (and `app_open` on IM) appends a sharing notice listing
  public/private tables, masked or unmarked PII, and PII found in the app HTML.
  The model must relay it.

## Rollout

- The numbered migration pair is `030_light_app_table_visibility.sql` /
  `.pg.sql`; `migrate.py` has an idempotent helper for databases that skipped
  the file.
- Existing extra tables become private in the migration; owners can opt them in
  from the dashboard or with `app_set_table_visibility`.
- The current app's PII columns can be marked with
  `app_alter_schema(pii={"emergency_contact": "phone", "id_tail": "id_card"})`
  or the dashboard schema endpoint.

## Tests

- `tests/unit/apps/test_pii.py` — detection/masking of free-text phone, ID,
  bank card, email; header classification.
- `tests/unit/apps/test_service.py` — write rejection, marking existing
  columns, auto-classification, free-text redaction, table visibility.
- `tests/unit/apps/test_tools.py` — visibility tool and share-link notice.
- `tests/integration/test_apps_api.py` — public table filtering, private-table
  404, PII marking API.
- `tests/unit/db/` — schema v30 migration and version-rewind idempotence.
