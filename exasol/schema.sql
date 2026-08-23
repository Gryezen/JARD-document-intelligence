-- ============================================================================
-- schema.sql
-- Exasol Personal Local — Agentic Document Intelligence Platform
--
-- Load with the starter kit's SQL CLI, e.g.:
--   exapump sql -p starter-kit -f schema.sql
--
-- Design notes:
--   - One SCHEMA (DOC_INTEL) holds all workflow tables. The read-only MCP
--     user created by the starter kit is granted SELECT on this schema only
--     (see docs/mcp-grants.sql), so the chat agent can never write.
--   - IDs are VARCHAR UUIDs generated in application code, not IDENTITY
--     columns, so ingestion/extraction/reasoning agents can construct related
--     rows (DOCUMENTS -> EXTRACTED_FIELDS -> DISCREPANCIES -> ACTIONS) before
--     any of them are committed, and so audit log rows can reference an
--     entity id that may not have finished writing yet.
--   - Every row that an agent produces gets a mirrored AUDIT_LOG entry.
--     Nothing in this schema should be treated as "the truth" unless it is
--     traceable back to an AUDIT_LOG row that explains how it got there.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS DOC_INTEL;
OPEN SCHEMA DOC_INTEL;
DROP TABLE IF EXISTS AUDIT_LOG;
DROP TABLE IF EXISTS HUMAN_REVIEWS;
DROP TABLE IF EXISTS ACTIONS;
DROP TABLE IF EXISTS DISCREPANCIES;
DROP TABLE IF EXISTS DOCUMENT_RELATIONSHIPS;
DROP TABLE IF EXISTS EXTRACTED_FIELDS;
DROP TABLE IF EXISTS DOCUMENTS;
DROP TABLE IF EXISTS CASES;

-- ----------------------------------------------------------------------------
-- CASES — a citizen/vendor's case file: the container a user explicitly
-- groups related uploaded documents into. Cross-document reasoning (see
-- DOCUMENT_RELATIONSHIPS / DISCREPANCIES below) is scoped to documents that
-- share a case, not to the whole registry.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE CASES (
    case_id              VARCHAR(36)   NOT NULL,          -- UUID
    name                 VARCHAR(300)  NOT NULL,
    created_by           VARCHAR(200),
    created_at           TIMESTAMP     NOT NULL,
    updated_at           TIMESTAMP     NOT NULL,
    report_summary       VARCHAR(4000),                   -- LLM-generated plain-language summary of the case's documents
    report_generated_at  TIMESTAMP,                        -- NULL until agents/report.py has run for this case
    CONSTRAINT PK_CASES PRIMARY KEY (case_id)
);

-- ----------------------------------------------------------------------------
-- DOCUMENTS — registry of every uploaded file and its processing state
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE DOCUMENTS (
    doc_id          VARCHAR(36)   NOT NULL,          -- UUID
    case_id         VARCHAR(36),                     -- the CASES row this document was uploaded into
    filename        VARCHAR(500)  NOT NULL,
    document_type   VARCHAR(100),                    -- e.g. 'invoice', 'land_record'; NULL until classified
    vendor          VARCHAR(300),                    -- vendor/citizen/entity name if known
    status          VARCHAR(50)   NOT NULL,          -- uploaded | extracting | review | reasoning | complete | failed
    source_path     VARCHAR(1000),                   -- storage location of the raw file
    page_count      DECIMAL(5,0),
    uploaded_by     VARCHAR(200),
    uploaded_at     TIMESTAMP     ,
    updated_at      TIMESTAMP     ,
    CONSTRAINT PK_DOCUMENTS PRIMARY KEY (doc_id),
    CONSTRAINT FK_DOCUMENTS_CASE FOREIGN KEY (case_id) REFERENCES CASES (case_id)
);

-- ----------------------------------------------------------------------------
-- EXTRACTED_FIELDS — structured field/value pairs pulled from a document
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE EXTRACTED_FIELDS (
    field_id        VARCHAR(36)   NOT NULL,
    doc_id          VARCHAR(36)   NOT NULL,
    field_name      VARCHAR(200)  NOT NULL,          -- e.g. 'invoice_amount', 'owner_name'
    field_value           VARCHAR(2000),
    confidence      DECIMAL(4,3),                    -- 0.000 - 1.000
    source_agent    VARCHAR(100),                    -- which extraction pass produced this
    extracted_at    TIMESTAMP     NOT NULL,
    CONSTRAINT PK_EXTRACTED_FIELDS PRIMARY KEY (field_id),
    CONSTRAINT FK_EXTRACTED_FIELDS_DOC FOREIGN KEY (doc_id) REFERENCES DOCUMENTS (doc_id)
);

-- ----------------------------------------------------------------------------
-- DOCUMENT_RELATIONSHIPS — links documents that should be reasoned about together
-- (invoice <-> PO <-> contract, or income-certificate <-> welfare-application)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE DOCUMENT_RELATIONSHIPS (
    relationship_id     VARCHAR(36)  NOT NULL,
    doc_id_1            VARCHAR(36)  NOT NULL,
    doc_id_2            VARCHAR(36)  NOT NULL,
    relationship_type   VARCHAR(100) NOT NULL,        -- e.g. 'invoice_to_po', 'income_cert_to_application'
    confidence          DECIMAL(4,3),
    created_at          TIMESTAMP    NOT NULL ,
    compared_at         TIMESTAMP,                    -- set once reasoning has run for this pair, so a
                                                        -- case-level re-run doesn't re-compare (and re-call
                                                        -- the model for) a pair that already has a result
    CONSTRAINT PK_DOCUMENT_RELATIONSHIPS PRIMARY KEY (relationship_id),
    CONSTRAINT FK_DOCREL_DOC1 FOREIGN KEY (doc_id_1) REFERENCES DOCUMENTS (doc_id),
    CONSTRAINT FK_DOCREL_DOC2 FOREIGN KEY (doc_id_2) REFERENCES DOCUMENTS (doc_id)
);

-- ----------------------------------------------------------------------------
-- DISCREPANCIES — normalized findings from the reasoning agent
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE DISCREPANCIES (
    discrepancy_id  VARCHAR(36)   NOT NULL,
    doc_id_1        VARCHAR(36)   NOT NULL,
    doc_id_2        VARCHAR(36),                      -- nullable: some discrepancies are single-document validation failures
    field_name      VARCHAR(200)  NOT NULL,
    value_1         VARCHAR(2000),
    value_2         VARCHAR(2000),
    severity        VARCHAR(20)   NOT NULL,           -- low | medium | high
    status          VARCHAR(50)   NOT NULL,  -- open | acknowledged | resolved | dismissed
    explanation     VARCHAR(2000),
    detected_at     TIMESTAMP     NOT NULL ,
    CONSTRAINT PK_DISCREPANCIES PRIMARY KEY (discrepancy_id),
    CONSTRAINT FK_DISCREPANCIES_DOC1 FOREIGN KEY (doc_id_1) REFERENCES DOCUMENTS (doc_id)
);

-- ----------------------------------------------------------------------------
-- ACTIONS — draft next-step proposals from the action agent (never auto-sent)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE ACTIONS (
    action_id       VARCHAR(36)   NOT NULL,
    discrepancy_id  VARCHAR(36),                       -- nullable: some actions aren't tied to a discrepancy
    doc_id          VARCHAR(36),
    action_type     VARCHAR(50)   NOT NULL,            -- email_draft | task_proposal
    content         VARCHAR(4000) NOT NULL,
    status          VARCHAR(50)   NOT NULL,  -- proposed | approved | rejected | sent
    created_at      TIMESTAMP     NOT NULL,
    decided_at       TIMESTAMP,
    decided_by       VARCHAR(200),
    CONSTRAINT PK_ACTIONS PRIMARY KEY (action_id),
    CONSTRAINT FK_ACTIONS_DISCREPANCY FOREIGN KEY (discrepancy_id) REFERENCES DISCREPANCIES (discrepancy_id),
    CONSTRAINT FK_ACTIONS_DOC FOREIGN KEY (doc_id) REFERENCES DOCUMENTS (doc_id)
);

-- ----------------------------------------------------------------------------
-- HUMAN_REVIEWS — corrections/approvals made by a human reviewer
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE HUMAN_REVIEWS (
    review_id       VARCHAR(36)   NOT NULL,
    doc_id          VARCHAR(36)   NOT NULL,
    field_id        VARCHAR(36),                       -- the EXTRACTED_FIELDS row being reviewed, if applicable
    field_name      VARCHAR(200),
    ai_value        VARCHAR(2000),
    human_value     VARCHAR(2000),
    status          VARCHAR(50)   NOT NULL,             -- confirmed | corrected | rejected
    reviewed_by     VARCHAR(200),
    reviewed_at     TIMESTAMP     NOT NULL,
    CONSTRAINT PK_HUMAN_REVIEWS PRIMARY KEY (review_id),
    CONSTRAINT FK_HUMAN_REVIEWS_DOC FOREIGN KEY (doc_id) REFERENCES DOCUMENTS (doc_id),
    CONSTRAINT FK_HUMAN_REVIEWS_FIELD FOREIGN KEY (field_id) REFERENCES EXTRACTED_FIELDS (field_id)
);

-- ----------------------------------------------------------------------------
-- AUDIT_LOG — explainable event history for every agent action in the pipeline
-- ----------------------------------------------------------------------------
CREATE OR REPLACE TABLE AUDIT_LOG (
    log_id          VARCHAR(36)   NOT NULL,
    doc_id          VARCHAR(36),                        -- nullable: chat-agent events aren't tied to one document
    agent_name      VARCHAR(100)  NOT NULL,             -- ingestion | extraction | confidence_gate | reasoning | action | chat | human
    action_name          VARCHAR(200)  NOT NULL,             -- short verb phrase, e.g. 'extracted_fields', 'flagged_discrepancy'
    input_summary   VARCHAR(2000),
    output_summary  VARCHAR(2000),
    confidence      DECIMAL(4,3),
    logged_at       TIMESTAMP     NOT NULL,
    CONSTRAINT PK_AUDIT_LOG PRIMARY KEY (log_id)
);

-- ----------------------------------------------------------------------------
-- Helpful indexes for the query patterns the chat agent will generate
-- (Exasol auto-indexes primary/foreign keys; these cover common filter columns)
-- ----------------------------------------------------------------------------
-- Exasol does not support CREATE INDEX explicitly (indexing is automatic),
-- so no manual index statements are needed here. Column choices above
-- (doc_id, field_name, status, severity) are kept narrow and typed for
-- predictable automatic indexing.
