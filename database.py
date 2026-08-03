"""SQLite persistence for imported statements and categorized transactions."""

import json
import os
import sqlite3
from pathlib import Path

import click
from flask import current_app, g


CATEGORY_NAMES = (
    "Groceries",
    "Dining",
    "Subscriptions",
    "Shopping",
    "Bills & Utilities",
    "Other",
)

DEFAULT_FILTERS = (
    ("payment thank you", "contains", None, 1),
    ("online payment", "contains", None, 1),
    ("automatic payment", "contains", None, 1),
    ("autopay", "contains", None, 1),
    ("mobile pymt", "contains", None, 1),
    ("pymtthank you", "contains", None, 1),
    ("mobile payment", "contains", None, 1),
    ("ach deposit", "contains", None, 1),
    ("grocery", "contains", "Groceries", 0),
    ("supermarket", "contains", "Groceries", 0),
    ("restaurant", "contains", "Dining", 0),
    ("food delivery", "contains", "Dining", 0),
    ("subscription", "contains", "Subscriptions", 0),
    ("streaming", "contains", "Subscriptions", 0),
    ("marketplace", "contains", "Shopping", 0),
    ("retail", "contains", "Shopping", 0),
    ("utility", "contains", "Bills & Utilities", 0),
    ("electric", "contains", "Bills & Utilities", 0),
    ("water", "contains", "Bills & Utilities", 0),
    ("internet", "contains", "Bills & Utilities", 0),
    ("energy", "contains", "Bills & Utilities", 0),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
    filename TEXT NOT NULL,
    issuer TEXT,
    statement_start TEXT,
    statement_end TEXT,
    extraction_method TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'confirmed')),
    warnings TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warnings)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    source_row INTEGER NOT NULL CHECK(source_row >= 0),
    transaction_date TEXT,
    original_description TEXT NOT NULL,
    merchant TEXT NOT NULL DEFAULT '',
    normalized_merchant TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(import_id, source_row)
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL COLLATE NOCASE,
    match_type TEXT NOT NULL CHECK(match_type IN ('exact', 'contains')),
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(pattern, match_type),
    CHECK(category_id IS NOT NULL OR excluded = 1)
);

CREATE INDEX IF NOT EXISTS idx_imports_status ON imports(status);
CREATE INDEX IF NOT EXISTS idx_transactions_import_order
    ON transactions(import_id, source_row);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_review ON transactions(needs_review)
    WHERE needs_review = 1;
CREATE INDEX IF NOT EXISTS idx_rules_category ON merchant_rules(category_id);
"""


def get_db():
    """Return one configured SQLite connection per Flask app context."""
    if "db" not in g:
        database_path = Path(current_app.config["DATABASE"])
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        database_path.parent.chmod(0o700)
        if not database_path.exists():
            descriptor = os.open(database_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
        database_path.chmod(0o600)
        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    _migrate_filters(db)
    if db.execute(
        "SELECT 1 FROM app_metadata WHERE key = 'defaults_seeded'"
    ).fetchone() is None:
        db.executemany(
            "INSERT OR IGNORE INTO categories(name) VALUES (?)",
            ((name,) for name in CATEGORY_NAMES),
        )
        db.executemany(
            """INSERT OR IGNORE INTO merchant_rules(
                   pattern, match_type, category_id, excluded
               ) VALUES (?, ?, (SELECT id FROM categories WHERE name = ?), ?)""",
            DEFAULT_FILTERS,
        )
        db.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('defaults_seeded', '1')"
        )
    db.commit()


def _migrate_filters(db):
    columns = db.execute("PRAGMA table_info(merchant_rules)").fetchall()
    category_column = next((row for row in columns if row["name"] == "category_id"), None)
    has_excluded = any(row["name"] == "excluded" for row in columns)
    category_foreign_key = next(
        (
            row
            for row in db.execute("PRAGMA foreign_key_list(merchant_rules)").fetchall()
            if row["from"] == "category_id"
        ),
        None,
    )
    if (
        has_excluded
        and not category_column["notnull"]
        and category_foreign_key is not None
        and category_foreign_key["on_delete"] == "CASCADE"
    ):
        return
    excluded_value = "excluded" if has_excluded else "0"
    db.executescript(
        f"""
        DROP INDEX IF EXISTS idx_rules_category;
        ALTER TABLE merchant_rules RENAME TO merchant_rules_legacy;
        CREATE TABLE merchant_rules (
            id INTEGER PRIMARY KEY,
            pattern TEXT NOT NULL COLLATE NOCASE,
            match_type TEXT NOT NULL CHECK(match_type IN ('exact', 'contains')),
            category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(pattern, match_type),
            CHECK(category_id IS NOT NULL OR excluded = 1)
        );
        INSERT INTO merchant_rules (
            id, pattern, match_type, category_id, excluded, created_at, updated_at
        )
        SELECT id, pattern, match_type, category_id, {excluded_value}, created_at, updated_at
        FROM merchant_rules_legacy;
        DROP TABLE merchant_rules_legacy;
        CREATE INDEX idx_rules_category ON merchant_rules(category_id);
        """
    )


@click.command("init-db")
def init_db_command():
    """Create the database tables and seed default categories and filters."""
    init_db()
    click.echo("Initialized the database.")


def init_app(app):
    """Register lifecycle hooks and initialize the configured database."""
    app.config.setdefault("DATABASE", str(Path(app.instance_path) / "app.sqlite3"))
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    with app.app_context():
        init_db()


def list_categories():
    return get_db().execute("SELECT * FROM categories ORDER BY id").fetchall()


def create_category(name):
    with get_db() as db:
        return db.execute("INSERT INTO categories(name) VALUES (?)", (name,)).lastrowid


def update_category(category_id, name):
    with get_db() as db:
        return db.execute(
            "UPDATE categories SET name = ? WHERE id = ?", (name, category_id)
        ).rowcount == 1


def delete_category(category_id):
    with get_db() as db:
        return db.execute("DELETE FROM categories WHERE id = ?", (category_id,)).rowcount == 1


def get_import(import_id):
    return get_db().execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()


def get_import_by_hash(content_sha256):
    return get_db().execute(
        "SELECT * FROM imports WHERE content_sha256 = ?", (content_sha256,)
    ).fetchone()


def list_imports(status=None):
    sql = "SELECT * FROM imports"
    params = ()
    if status is not None:
        sql += " WHERE status = ?"
        params = (status,)
    return get_db().execute(sql + " ORDER BY created_at DESC, id DESC", params).fetchall()


def _warnings_json(warnings):
    if warnings is None:
        return "[]"
    if isinstance(warnings, str):
        json.loads(warnings)
        return warnings
    return json.dumps(warnings, separators=(",", ":"))


def _transaction_values(import_id, transaction, source_row=None):
    row_number = transaction.get("source_row", source_row)
    if row_number is None:
        raise ValueError("each transaction requires source_row")
    return (
        import_id,
        row_number,
        transaction.get("transaction_date", transaction.get("date")),
        transaction["original_description"],
        transaction.get("merchant", ""),
        transaction.get("normalized_merchant", ""),
        transaction["amount_cents"],
        transaction.get("category_id"),
        bool(transaction.get("excluded", False)),
        bool(transaction.get("needs_review", transaction.get("review", False))),
    )


def _insert_transactions(db, import_id, transactions):
    db.executemany(
        """INSERT INTO transactions (
               import_id, source_row, transaction_date, original_description,
               merchant, normalized_merchant, amount_cents, category_id,
               excluded, needs_review
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            _transaction_values(import_id, transaction, index)
            for index, transaction in enumerate(transactions)
        ),
    )


def create_import(
    content_sha256,
    filename,
    transactions=(),
    *,
    issuer=None,
    statement_start=None,
    statement_end=None,
    extraction_method=None,
    warnings=None,
    status="draft",
):
    """Atomically create an import and all of its source-ordered transactions."""
    db = get_db()
    with db:
        cursor = db.execute(
            """INSERT INTO imports (
                   content_sha256, filename, issuer, statement_start, statement_end,
                   extraction_method, status, warnings, confirmed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'confirmed'
                   THEN CURRENT_TIMESTAMP END)""",
            (
                content_sha256,
                filename,
                issuer,
                statement_start,
                statement_end,
                extraction_method,
                status,
                _warnings_json(warnings),
                status,
            ),
        )
        import_id = cursor.lastrowid
        _insert_transactions(db, import_id, transactions)
    return import_id


def replace_draft_transactions(import_id, transactions, *, warnings=None):
    """Atomically replace parsed rows for an import that is still a draft."""
    db = get_db()
    with db:
        draft = db.execute(
            "SELECT 1 FROM imports WHERE id = ? AND status = 'draft'", (import_id,)
        ).fetchone()
        if draft is None:
            raise ValueError("import does not exist or is not a draft")
        db.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
        _insert_transactions(db, import_id, transactions)
        if warnings is None:
            db.execute(
                "UPDATE imports SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (import_id,),
            )
        else:
            db.execute(
                """UPDATE imports SET warnings = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (_warnings_json(warnings), import_id),
            )


def confirm_import(import_id):
    db = get_db()
    with db:
        cursor = db.execute(
            """UPDATE imports SET status = 'confirmed',
                   confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'draft'""",
            (import_id,),
        )
        return cursor.rowcount == 1


def delete_import(import_id):
    db = get_db()
    with db:
        return db.execute("DELETE FROM imports WHERE id = ?", (import_id,)).rowcount == 1


def update_transaction(transaction_id, **changes):
    """Update user-editable transaction fields and return whether a row changed."""
    allowed = {
        "transaction_date",
        "merchant",
        "normalized_merchant",
        "amount_cents",
        "category_id",
        "excluded",
        "needs_review",
    }
    unknown = changes.keys() - allowed
    if unknown:
        raise ValueError(f"unsupported transaction fields: {', '.join(sorted(unknown))}")
    if not changes:
        return False
    assignments = ", ".join(f"{field} = ?" for field in changes)
    db = get_db()
    with db:
        cursor = db.execute(
            f"UPDATE transactions SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (*changes.values(), transaction_id),
        )
        return cursor.rowcount == 1


def list_transactions(
    *, import_id=None, status=None, category_id=None, include_excluded=True,
    query=None, date_from=None, date_to=None
):
    clauses = []
    params = []
    if import_id is not None:
        clauses.append("t.import_id = ?")
        params.append(import_id)
    if status is not None:
        clauses.append("i.status = ?")
        params.append(status)
    if category_id is not None:
        clauses.append("t.category_id = ?")
        params.append(category_id)
    if not include_excluded:
        clauses.append("t.excluded = 0")
    if query:
        clauses.append("(t.merchant LIKE ? OR t.original_description LIKE ?)")
        params.extend((f"%{query}%", f"%{query}%"))
    if date_from:
        clauses.append("t.transaction_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("t.transaction_date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return get_db().execute(
        """SELECT t.*, c.name AS category_name, i.status AS import_status
           FROM transactions t
           JOIN imports i ON i.id = t.import_id
           LEFT JOIN categories c ON c.id = t.category_id"""
        + where
        + " ORDER BY t.transaction_date DESC, t.import_id DESC, t.source_row",
        params,
    ).fetchall()


def transaction_summary(*, import_id=None, status="confirmed", include_excluded=False):
    clauses = []
    params = []
    if import_id is not None:
        clauses.append("t.import_id = ?")
        params.append(import_id)
    if status is not None:
        clauses.append("i.status = ?")
        params.append(status)
    if not include_excluded:
        clauses.append("t.excluded = 0")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return get_db().execute(
        """SELECT c.id AS category_id, COALESCE(c.name, 'Uncategorized') AS category,
                  COUNT(*) AS transaction_count, SUM(t.amount_cents) AS amount_cents
           FROM transactions t
           JOIN imports i ON i.id = t.import_id
           LEFT JOIN categories c ON c.id = t.category_id"""
        + where
        + " GROUP BY c.id, c.name ORDER BY amount_cents DESC, category",
        params,
    ).fetchall()


def get_transaction(transaction_id):
    return get_db().execute(
        "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
    ).fetchone()


def list_filters():
    return get_db().execute(
        """SELECT r.*, c.name AS category_name FROM merchant_rules r
           LEFT JOIN categories c ON c.id = r.category_id
           ORDER BY CASE r.match_type WHEN 'exact' THEN 0 ELSE 1 END,
                    length(r.pattern) DESC, r.pattern"""
    ).fetchall()


def create_filter(pattern, match_type, category_id=None, excluded=False):
    if match_type not in {"exact", "contains"}:
        raise ValueError("match_type must be 'exact' or 'contains'")
    if category_id is None and not excluded:
        raise ValueError("a filter must categorize or exclude transactions")
    db = get_db()
    with db:
        return db.execute(
            """INSERT INTO merchant_rules(pattern, match_type, category_id, excluded)
               VALUES (?, ?, ?, ?)""",
            (pattern, match_type, category_id, bool(excluded)),
        ).lastrowid


def update_filter(filter_id, *, pattern, match_type, category_id=None, excluded=False):
    if match_type not in {"exact", "contains"}:
        raise ValueError("match_type must be 'exact' or 'contains'")
    if category_id is None and not excluded:
        raise ValueError("a filter must categorize or exclude transactions")
    db = get_db()
    with db:
        cursor = db.execute(
            """UPDATE merchant_rules SET pattern = ?, match_type = ?, category_id = ?,
                   excluded = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (pattern, match_type, category_id, bool(excluded), filter_id),
        )
        return cursor.rowcount == 1


def delete_filter(filter_id):
    db = get_db()
    with db:
        return db.execute(
            "DELETE FROM merchant_rules WHERE id = ?", (filter_id,)
        ).rowcount == 1
