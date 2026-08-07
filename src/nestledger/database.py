"""SQLite persistence for imported statements and categorized transactions."""

import json
import os
import sqlite3
from pathlib import Path

import click
from flask import current_app, g

from .strategy_import import ParsedCategory, ParsedHolding, ParsedStrategy


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
    card_name TEXT,
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

CREATE TABLE IF NOT EXISTS ira_strategies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    risk_score INTEGER NOT NULL DEFAULT 5 CHECK(risk_score BETWEEN 1 AND 10),
    source_filename TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ira_strategy_categories (
    id INTEGER PRIMARY KEY,
    strategy_id INTEGER NOT NULL REFERENCES ira_strategies(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    position INTEGER NOT NULL CHECK(position >= 0),
    UNIQUE(strategy_id, name),
    UNIQUE(strategy_id, position)
);

CREATE TABLE IF NOT EXISTS ira_strategy_holdings (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL
        REFERENCES ira_strategy_categories(id) ON DELETE CASCADE,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL DEFAULT '',
    allocation_basis_points INTEGER NOT NULL
        CHECK(allocation_basis_points >= 0 AND allocation_basis_points <= 10000),
    position INTEGER NOT NULL CHECK(position >= 0),
    UNIQUE(category_id, position)
);

CREATE TABLE IF NOT EXISTS ira_strategy_import_batches (
    id INTEGER PRIMARY KEY,
    replacement_strategy_id INTEGER REFERENCES ira_strategies(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ira_strategy_import_drafts (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL
        REFERENCES ira_strategy_import_batches(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    name TEXT NOT NULL COLLATE NOCASE,
    source_filename TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    UNIQUE(batch_id, position),
    UNIQUE(batch_id, name)
);

CREATE INDEX IF NOT EXISTS idx_imports_status ON imports(status);
CREATE INDEX IF NOT EXISTS idx_transactions_import_order
    ON transactions(import_id, source_row);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_transactions_review ON transactions(needs_review)
    WHERE needs_review = 1;
CREATE INDEX IF NOT EXISTS idx_rules_category ON merchant_rules(category_id);
CREATE INDEX IF NOT EXISTS idx_ira_categories_strategy
    ON ira_strategy_categories(strategy_id, position);
CREATE INDEX IF NOT EXISTS idx_ira_holdings_category
    ON ira_strategy_holdings(category_id, position);
CREATE INDEX IF NOT EXISTS idx_ira_strategy_drafts_batch
    ON ira_strategy_import_drafts(batch_id, position);
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
    _migrate_imports(db)
    _migrate_filters(db)
    _migrate_ira_strategies(db)
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


def _migrate_imports(db):
    columns = db.execute("PRAGMA table_info(imports)").fetchall()
    if not any(row["name"] == "card_name" for row in columns):
        db.execute("ALTER TABLE imports ADD COLUMN card_name TEXT")


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


def _migrate_ira_strategies(db):
    columns = db.execute("PRAGMA table_info(ira_strategies)").fetchall()
    if not any(row["name"] == "risk_score" for row in columns):
        db.execute(
            """ALTER TABLE ira_strategies ADD COLUMN risk_score INTEGER
               NOT NULL DEFAULT 5 CHECK(risk_score BETWEEN 1 AND 10)"""
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


def list_ira_strategies():
    return get_db().execute(
        """SELECT s.*, COUNT(h.id) AS holding_count
           FROM ira_strategies s
           LEFT JOIN ira_strategy_categories c ON c.strategy_id = s.id
           LEFT JOIN ira_strategy_holdings h ON h.category_id = c.id
           GROUP BY s.id
           ORDER BY s.risk_score, s.name COLLATE NOCASE, s.id"""
    ).fetchall()


def get_ira_strategy(strategy_id):
    strategy = get_db().execute(
        "SELECT * FROM ira_strategies WHERE id = ?", (strategy_id,)
    ).fetchone()
    return _hydrate_ira_strategy(strategy)


def get_ira_strategy_by_slug(slug):
    strategy = get_db().execute(
        "SELECT * FROM ira_strategies WHERE slug = ? COLLATE NOCASE", (slug,)
    ).fetchone()
    return _hydrate_ira_strategy(strategy)


def get_ira_strategy_by_name(name):
    return get_db().execute(
        "SELECT * FROM ira_strategies WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def _hydrate_ira_strategy(strategy):
    if strategy is None:
        return None
    rows = get_db().execute(
        """SELECT c.id AS category_id, c.name AS category_name,
                  c.position AS category_position, h.id AS holding_id,
                  h.asset, h.ticker, h.allocation_basis_points,
                  h.position AS holding_position
           FROM ira_strategy_categories c
           JOIN ira_strategy_holdings h ON h.category_id = c.id
           WHERE c.strategy_id = ?
           ORDER BY c.position, h.position""",
        (strategy["id"],),
    ).fetchall()
    categories = []
    category_lookup = {}
    holdings = []
    for row in rows:
        category = category_lookup.get(row["category_id"])
        if category is None:
            category = {
                "id": row["category_id"],
                "name": row["category_name"],
                "allocation": 0.0,
                "holdings": [],
            }
            category_lookup[row["category_id"]] = category
            categories.append(category)
        holding = {
            "id": row["holding_id"],
            "category": row["category_name"],
            "asset": row["asset"],
            "ticker": row["ticker"],
            "allocation_bps": row["allocation_basis_points"],
            "allocation": row["allocation_basis_points"] / 100,
        }
        category["holdings"].append(holding)
        category["allocation"] += holding["allocation"]
        holdings.append(holding)
    return {
        "id": strategy["id"],
        "name": strategy["name"],
        "strategy": strategy["name"],
        "slug": strategy["slug"],
        "risk_score": strategy["risk_score"],
        "source_filename": strategy["source_filename"],
        "created_at": strategy["created_at"],
        "updated_at": strategy["updated_at"],
        "categories": categories,
        "category_groups": categories,
        "holdings": holdings,
    }


def _insert_ira_strategy_rows(db, strategy_id, parsed_strategy):
    for category_position, category in enumerate(parsed_strategy.categories):
        category_id = db.execute(
            """INSERT INTO ira_strategy_categories(strategy_id, name, position)
               VALUES (?, ?, ?)""",
            (strategy_id, category.name, category_position),
        ).lastrowid
        db.executemany(
            """INSERT INTO ira_strategy_holdings(
                   category_id, asset, ticker, allocation_basis_points, position
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                (category_id, holding.asset, holding.ticker,
                 holding.allocation_bps, position)
                for position, holding in enumerate(category.holdings)
            ),
        )


def _validate_ira_strategy(parsed_strategy):
    if not parsed_strategy.name or len(parsed_strategy.name) > 120:
        raise ValueError("strategy name must contain 1 to 120 characters")
    category_names = set()
    holding_count = 0
    total_bps = 0
    for category in parsed_strategy.categories:
        category_key = category.name.casefold()
        if not category.name or len(category.name) > 120 or category_key in category_names:
            raise ValueError("strategy categories must have unique names")
        category_names.add(category_key)
        if not category.holdings:
            raise ValueError("strategy categories must contain holdings")
        for holding in category.holdings:
            if not holding.asset or len(holding.asset) > 120 or len(holding.ticker) > 32:
                raise ValueError("strategy holdings contain invalid text")
            if not 0 <= holding.allocation_bps <= 10000:
                raise ValueError("strategy allocations must be between 0 and 100 percent")
            holding_count += 1
            total_bps += holding.allocation_bps
    if not category_names or holding_count > 500 or len(category_names) > 100:
        raise ValueError("strategy must contain between 1 and 500 holdings")
    if total_bps != 10000:
        raise ValueError("strategy allocations must total 100.00 percent")


def _create_ira_strategy(db, parsed_strategy, slug, source_filename, risk_score):
    strategy_id = db.execute(
        """INSERT INTO ira_strategies(name, slug, risk_score, source_filename)
           VALUES (?, ?, ?, ?)""",
        (parsed_strategy.name, slug, risk_score, source_filename),
    ).lastrowid
    _insert_ira_strategy_rows(db, strategy_id, parsed_strategy)
    return strategy_id


def create_ira_strategy(parsed_strategy, slug, source_filename=None, risk_score=5):
    _validate_ira_strategy(parsed_strategy)
    if not 1 <= risk_score <= 10:
        raise ValueError("risk score must be between 1 and 10")
    db = get_db()
    with db:
        return _create_ira_strategy(
            db, parsed_strategy, slug, source_filename, risk_score
        )


def create_ira_strategy_import_batch(strategies, replacement_strategy_id=None):
    if not strategies:
        raise ValueError("strategy import batch cannot be empty")
    for parsed_strategy, _ in strategies:
        _validate_ira_strategy(parsed_strategy)
    db = get_db()
    with db:
        batch_id = db.execute(
            """INSERT INTO ira_strategy_import_batches(replacement_strategy_id)
               VALUES (?)""",
            (replacement_strategy_id,),
        ).lastrowid
        for position, (parsed_strategy, source_filename) in enumerate(strategies):
            payload = json.dumps(
                {
                    "categories": [
                        {
                            "name": category.name,
                            "holdings": [
                                {
                                    "asset": holding.asset,
                                    "ticker": holding.ticker,
                                    "allocation_bps": holding.allocation_bps,
                                }
                                for holding in category.holdings
                            ],
                        }
                        for category in parsed_strategy.categories
                    ]
                },
                separators=(",", ":"),
            )
            db.execute(
                """INSERT INTO ira_strategy_import_drafts(
                       batch_id, position, name, source_filename, payload
                   ) VALUES (?, ?, ?, ?, ?)""",
                (batch_id, position, parsed_strategy.name, source_filename, payload),
            )
    return batch_id


def get_ira_strategy_import_batch(batch_id):
    batch = get_db().execute(
        "SELECT * FROM ira_strategy_import_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        return None
    output = dict(batch)
    output["items"] = []
    rows = get_db().execute(
        """SELECT * FROM ira_strategy_import_drafts
           WHERE batch_id = ? ORDER BY position""",
        (batch_id,),
    ).fetchall()
    for row in rows:
        item = dict(row)
        payload = json.loads(item.pop("payload"))
        item["parsed"] = ParsedStrategy(
            item["name"],
            tuple(
                ParsedCategory(
                    category["name"],
                    tuple(
                        ParsedHolding(
                            holding["asset"],
                            holding["ticker"],
                            holding["allocation_bps"],
                        )
                        for holding in category["holdings"]
                    ),
                )
                for category in payload["categories"]
            ),
        )
        item["holding_count"] = sum(
            len(category.holdings) for category in item["parsed"].categories
        )
        output["items"].append(item)
    return output


def list_ira_strategy_import_batches():
    return get_db().execute(
        """SELECT b.id, b.replacement_strategy_id, b.created_at,
                  COUNT(d.id) AS strategy_count,
                  GROUP_CONCAT(d.name, ', ') AS strategy_names
           FROM ira_strategy_import_batches b
           JOIN ira_strategy_import_drafts d ON d.batch_id = b.id
           GROUP BY b.id
           ORDER BY b.id DESC"""
    ).fetchall()


def confirm_ira_strategy_import_batch(batch_id, strategies):
    batch = get_ira_strategy_import_batch(batch_id)
    if batch is None or len(batch["items"]) != len(strategies):
        raise ValueError("strategy import draft does not exist")
    if [item["id"] for item in batch["items"]] != [item_id for item_id, *_ in strategies]:
        raise ValueError("strategy import draft does not match submitted strategies")
    for _, parsed_strategy, _, _, risk_score in strategies:
        _validate_ira_strategy(parsed_strategy)
        if not 1 <= risk_score <= 10:
            raise ValueError("risk score must be between 1 and 10")

    db = get_db()
    with db:
        replacement_id = batch["replacement_strategy_id"]
        if replacement_id is not None:
            if len(strategies) != 1:
                raise ValueError("replacement drafts must contain one strategy")
            _, parsed_strategy, _, source_filename, risk_score = strategies[0]
            updated = db.execute(
                """UPDATE ira_strategies SET name = ?, source_filename = ?,
                          risk_score = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (parsed_strategy.name, source_filename, risk_score, replacement_id),
            ).rowcount
            if not updated:
                raise ValueError("strategy being replaced no longer exists")
            db.execute(
                "DELETE FROM ira_strategy_categories WHERE strategy_id = ?",
                (replacement_id,),
            )
            _insert_ira_strategy_rows(db, replacement_id, parsed_strategy)
        else:
            for _, parsed_strategy, slug, source_filename, risk_score in strategies:
                _create_ira_strategy(
                    db, parsed_strategy, slug, source_filename, risk_score
                )
        db.execute("DELETE FROM ira_strategy_import_batches WHERE id = ?", (batch_id,))


def delete_ira_strategy_import_batch(batch_id):
    with get_db() as db:
        return db.execute(
            "DELETE FROM ira_strategy_import_batches WHERE id = ?", (batch_id,)
        ).rowcount == 1


def replace_ira_strategy(
    strategy_id, parsed_strategy, *, source_filename=None, update_source=False,
    risk_score=None
):
    _validate_ira_strategy(parsed_strategy)
    if risk_score is not None and not 1 <= risk_score <= 10:
        raise ValueError("risk score must be between 1 and 10")
    db = get_db()
    with db:
        strategy = db.execute(
            "SELECT 1 FROM ira_strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        if strategy is None:
            return False
        assignments = "name = ?, updated_at = CURRENT_TIMESTAMP"
        params = [parsed_strategy.name]
        if update_source:
            assignments += ", source_filename = ?"
            params.append(source_filename)
        if risk_score is not None:
            assignments += ", risk_score = ?"
            params.append(risk_score)
        params.append(strategy_id)
        db.execute(
            f"UPDATE ira_strategies SET {assignments} WHERE id = ?", params
        )
        db.execute(
            "DELETE FROM ira_strategy_categories WHERE strategy_id = ?",
            (strategy_id,),
        )
        _insert_ira_strategy_rows(db, strategy_id, parsed_strategy)
    return True


def delete_ira_strategy(strategy_id):
    with get_db() as db:
        return db.execute(
            "DELETE FROM ira_strategies WHERE id = ?", (strategy_id,)
        ).rowcount == 1


def ira_strategy_slug_exists(slug):
    return get_db().execute(
        "SELECT 1 FROM ira_strategies WHERE slug = ? COLLATE NOCASE", (slug,)
    ).fetchone() is not None


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


def list_card_names(status="confirmed"):
    clauses = ["card_name IS NOT NULL", "trim(card_name) != ''"]
    params = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    return [
        row["card_name"]
        for row in get_db().execute(
            "SELECT DISTINCT card_name FROM imports WHERE "
            + " AND ".join(clauses)
            + " ORDER BY card_name COLLATE NOCASE",
            params,
        ).fetchall()
    ]


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
    card_name=None,
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
                   content_sha256, filename, card_name, issuer, statement_start, statement_end,
                   extraction_method, status, warnings, confirmed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'confirmed'
                    THEN CURRENT_TIMESTAMP END)""",
            (
                content_sha256,
                filename,
                card_name,
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
    query=None, card_name=None, date_from=None, date_to=None
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
    if card_name:
        clauses.append("i.card_name = ?")
        params.append(card_name)
    if date_from:
        clauses.append("t.transaction_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("t.transaction_date <= ?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return get_db().execute(
        """SELECT t.*, c.name AS category_name, i.status AS import_status,
                  i.card_name AS card_name
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
