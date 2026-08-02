import csv
from pathlib import Path

from flask import Flask, abort, redirect, render_template, url_for


BASE_DIR = Path(__file__).resolve().parent
STRATEGIES_DIR = BASE_DIR / "strategies"
REQUIRED_COLUMNS = {"Strategy", "Category", "Asset", "Ticker", "Allocation"}

app = Flask(__name__)


def load_portfolios():
    portfolios = []
    errors = []

    for csv_path in STRATEGIES_DIR.glob("*.csv"):
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
                    missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
                    raise ValueError(f"missing columns: {', '.join(missing)}")

                holdings = []
                strategy = ""
                for row_number, row in enumerate(reader, start=2):
                    if row["Category"].strip().lower() == "total":
                        continue

                    strategy = strategy or row["Strategy"].strip()

                    allocation_text = row["Allocation"].strip().removesuffix("%")
                    try:
                        allocation = float(allocation_text)
                    except ValueError as error:
                        raise ValueError(
                            f"invalid allocation on row {row_number}"
                        ) from error

                    if allocation < 0:
                        raise ValueError(f"negative allocation on row {row_number}")

                    holdings.append(
                        {
                            "category": row["Category"].strip(),
                            "asset": row["Asset"].strip(),
                            "ticker": row["Ticker"].strip(),
                            "allocation": allocation,
                        }
                    )

                if not holdings:
                    raise ValueError("no holdings found")

                total = sum(holding["allocation"] for holding in holdings)
                if abs(total - 100) > 0.01:
                    raise ValueError(f"allocations total {total:.2f}%, not 100%")

                strategy = strategy or csv_path.stem.replace("_", " ").title()
                portfolios.append(
                    {
                        "slug": csv_path.stem,
                        "strategy": strategy,
                        "holdings": holdings,
                    }
                )
        except (OSError, ValueError, csv.Error) as error:
            errors.append(f"{csv_path.name}: {error}")

    risk_order = {"low": 0, "medium": 1, "high": 2}
    portfolios.sort(
        key=lambda portfolio: (
            risk_order.get(portfolio["strategy"].split()[0].lower(), 99),
            portfolio["strategy"].lower(),
        )
    )
    return portfolios, errors


@app.route("/")
def index():
    portfolios, errors = load_portfolios()
    if portfolios:
        return redirect(url_for("portfolio", slug=portfolios[0]["slug"]))
    return render_template("index.html", portfolios=[], portfolio=None, errors=errors)


@app.route("/portfolio/<slug>")
def portfolio(slug):
    portfolios, errors = load_portfolios()
    selected = next((item for item in portfolios if item["slug"] == slug), None)
    if selected is None:
        abort(404)

    categories = {}
    category_groups = {}
    for holding in selected["holdings"]:
        categories[holding["category"]] = (
            categories.get(holding["category"], 0) + holding["allocation"]
        )
        category_groups.setdefault(holding["category"], []).append(holding)

    selected["categories"] = [
        {"name": name, "allocation": allocation}
        for name, allocation in categories.items()
    ]
    selected["category_groups"] = [
        {
            "name": name,
            "allocation": categories[name],
            "holdings": holdings,
        }
        for name, holdings in category_groups.items()
    ]
    return render_template(
        "index.html", portfolios=portfolios, portfolio=selected, errors=errors
    )


if __name__ == "__main__":
    app.run(debug=True)
