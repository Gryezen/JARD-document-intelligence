-- scripts/reset_comparisons.sql
--
-- One-time cleanup for data produced by the pre-fix duplicate-comparison
-- bug (compare_related_documents() used to re-run reasoning on already-
-- compared pairs, and one action was drafted per discrepancy instead of
-- one per pair — see agents/action.py / orchestration/workflow.py).
--
-- This clears discrepancies + drafted actions and resets comparison state
-- so the next run regenerates clean, deduped results with the fixed code.
-- Safe to run any time the DISCREPANCIES/ACTIONS tables only hold data
-- you're OK regenerating (e.g. a demo/test run) — it does not touch
-- DOCUMENTS, EXTRACTED_FIELDS, or CASES.
--
-- Run with the same CLI you used for schema.sql, e.g.:
--   exapump sql -p starter-kit -f scripts/reset_comparisons.sql

OPEN SCHEMA DOC_INTEL;

DELETE FROM ACTIONS;
DELETE FROM DISCREPANCIES;
UPDATE DOCUMENT_RELATIONSHIPS SET compared_at = NULL;
UPDATE DOCUMENTS SET status = 'reasoning' WHERE status = 'complete';
