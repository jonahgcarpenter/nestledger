# IRA Strategys

A personal Flask app that loads CSV-formatted portfolio strategies from `strategies/` and simplifies monthly contribution planning and portfolio rebalancing.

## Run

```bash
# Create the virtual enviornment
python -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependancies
pip install -r requirements.txt

# Start flask server
python app.py
```

Open `http://127.0.0.1:5000` in a browser.

## Add A Portfolio

Place a `.csv` file in `strategies/` with this format:

```csv
Strategy,Category,Asset,Ticker,Allocation
High risk,US stocks,iShares Core S&P 500 ETF,IVV,27.00%
High risk,US stocks,Vanguard Russell 1000 Growth ETF,VONG,18.00%
High risk,Total,,,100.00%
```

Allocations excluding the optional `Total` row must add up to 100%. The strategy appears in the navbar automatically.
