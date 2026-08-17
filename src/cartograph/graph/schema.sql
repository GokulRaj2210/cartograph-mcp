-- Cartograph code-graph schema.
--
-- Design notes:
--   * Everything is one SQLite file so the index is trivially cacheable in CI and
--     shippable next to the repo. No server, no embeddings, no network.
--   * Traversal (who-calls / calls / blast-radius) is done with recursive CTEs
--     directly against `edges`, so query cost stays in C rather than Python.
--   * `edges.confidence` is first-class: name-based resolution is inherently
--     ambiguous, and callers get to choose their own precision/recall tradeoff
--     instead of being handed a fake certainty.

-- NOTE: connection pragmas deliberately do NOT live here. `journal_mode` needs
-- an exclusive lock, so running it on every open made a *read* fail whenever
-- anything else had the database open -- e.g. reindexing while an agent's MCP
-- server was live. Persistent pragmas are set once at creation and per-connection
-- pragmas on each connect; see GraphStore.open / _create_schema.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT    NOT NULL UNIQUE,   -- repo-relative, POSIX separators
    lang       TEXT    NOT NULL,
    module     TEXT    NOT NULL,          -- language-agnostic module key (see indexer.modules)
    sha256     TEXT    NOT NULL,          -- drives incremental reindexing
    size       INTEGER NOT NULL,
    lines      INTEGER NOT NULL,
    is_test    INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_module ON files (module);
CREATE INDEX IF NOT EXISTS idx_files_lang ON files (lang);

CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES symbols (id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,          -- local name, e.g. "who_calls"
    qualname   TEXT    NOT NULL,          -- e.g. "graph.store.GraphStore.who_calls"
    kind       TEXT    NOT NULL,          -- function|method|class|interface|struct|enum|type|const
    lang       TEXT    NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    signature  TEXT,
    docstring  TEXT,
    exported   INTEGER NOT NULL DEFAULT 1,
    rank       REAL    NOT NULL DEFAULT 0.0   -- PageRank over the call graph
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols (name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualname ON symbols (qualname);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols (file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols (kind);
CREATE INDEX IF NOT EXISTS idx_symbols_rank ON symbols (rank DESC);

CREATE TABLE IF NOT EXISTS edges (
    id         INTEGER PRIMARY KEY,
    src_id     INTEGER NOT NULL REFERENCES symbols (id) ON DELETE CASCADE,
    dst_id     INTEGER REFERENCES symbols (id) ON DELETE CASCADE,  -- NULL = unresolved
    dst_name   TEXT    NOT NULL,          -- raw callee text, kept even when resolved
    kind       TEXT    NOT NULL,          -- calls|inherits|instantiates
    line       INTEGER NOT NULL,
    confidence REAL    NOT NULL DEFAULT 1.0,
    reason     TEXT    NOT NULL           -- which resolution rule fired (auditable)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst_name ON edges (dst_name);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    raw         TEXT    NOT NULL,         -- as written in source
    module      TEXT    NOT NULL,         -- normalised module key
    symbol      TEXT,                     -- named import, when the syntax provides one
    alias       TEXT,
    dst_file_id INTEGER REFERENCES files (id) ON DELETE SET NULL,
    external    INTEGER NOT NULL DEFAULT 0,
    line        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_imports_file ON imports (file_id);
CREATE INDEX IF NOT EXISTS idx_imports_dst ON imports (dst_file_id);
CREATE INDEX IF NOT EXISTS idx_imports_symbol ON imports (symbol);

-- Raw, unresolved use-sites. These are *facts* straight out of the parser and
-- are stored per-file so incremental indexing can replace a single file's rows.
-- `edges` is then recomputed as a pure function of (references x symbols) on
-- every index run, which is what makes it impossible for an incremental reindex
-- to leave a stale edge pointing at a symbol that moved or vanished.
CREATE TABLE IF NOT EXISTS refs (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    src_local TEXT,                       -- NULL = module/top-level scope
    dst_name  TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    line      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refs_file ON refs (file_id);

-- Contentless FTS5: we own the row ids, so we store no duplicate text and
-- rebuild the index per-file on reindex.
CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5 (
    name,
    qualname,
    signature,
    docstring,
    content = '',
    tokenize = 'unicode61 remove_diacritics 2'
);
