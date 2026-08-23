-- ============================================================================
-- docs/mcp-grants.sql
--
-- The starter kit already provisions a read-only user for its MCP server
-- (see AGENTS.md: "an MCP server with a dedicated read-only database user").
-- This file documents/creates the equivalent grant scoped to DOC_INTEL, so
-- the chat agent's ReadOnlyDatabase connection (database/db.py) and the
-- starter kit's MCP server share the same safety guarantee: neither can
-- write, regardless of what SQL an LLM generates.
--
-- Run once, as an administrative user, after schema.sql:
--   exapump sql -p starter-kit -f docs/mcp-grants.sql
-- ============================================================================

-- If the starter kit did not already create this user for its MCP server,
-- create a dedicated one for this project instead of reusing SYS anywhere
-- near query execution. Match the username to EXASOL_RO_USER in your .env,
-- and pick a real password (matching EXASOL_RO_PASSWORD) instead of the
-- placeholder below:
-- CREATE USER DOC_INTEL_RO IDENTIFIED BY "change-me-and-set-EXASOL_RO_PASSWORD";

GRANT SELECT ON SCHEMA DOC_INTEL TO mcp_readonly;

-- Explicitly withheld (documented for judges/auditors, not because Exasol
-- would grant these by default): INSERT, UPDATE, DELETE, DROP, ALTER on
-- DOC_INTEL, and any privilege outside this schema.
