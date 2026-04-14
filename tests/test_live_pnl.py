"""Tests for live PnL calculation from HL fills."""

from live.pnl import calc_pnl_from_fills


# Real data from HL CSV: Trade 1 (2026-04-03)
# Open Long BTC @ 66480, sz=0.00793, fee=0.075914, closedPnl=-0.075914
# Close Long BTC @ 66877, sz=0.00793, fee=0.229104, closedPnl=2.919106
TRADE_1_FILLS = [
    {
        "coin": "BTC",
        "dir": "Open Long",
        "px": "66480",
        "sz": "0.00793",
        "fee": "0.075914",
        "closedPnl": "-0.075914",
        "time": 1743695414000,
    },
    {
        "coin": "BTC",
        "dir": "Close Long",
        "px": "66877",
        "sz": "0.00793",
        "fee": "0.229104",
        "closedPnl": "2.919106",
        "time": 1743697815000,
    },
]

# Trade 2: Open Short @ 67111, Close Short @ 67201 (loser)
TRADE_2_FILLS = [
    {
        "coin": "BTC",
        "dir": "Open Short",
        "px": "67111",
        "sz": "0.01462",
        "fee": "0.141287",
        "closedPnl": "-0.141287",
        "time": 1743767539000,
    },
    {
        "coin": "BTC",
        "dir": "Close Short",
        "px": "67201",
        "sz": "0.01462",
        "fee": "0.42443",
        "closedPnl": "-1.74023",
        "time": 1743777603000,
    },
]

# Trade 5 (multiple open fills): Open Short in 5 fills, close in 1
TRADE_5_MULTI_FILLS = [
    {"coin": "BTC", "dir": "Open Short", "px": "67653", "sz": "0.0013",
     "fee": "0.012664", "closedPnl": "-0.012664", "time": 1743893404000},
    {"coin": "BTC", "dir": "Open Short", "px": "67653", "sz": "0.00024",
     "fee": "0.002338", "closedPnl": "-0.002338", "time": 1743893404000},
    {"coin": "BTC", "dir": "Open Short", "px": "67653", "sz": "0.00147",
     "fee": "0.01432", "closedPnl": "-0.01432", "time": 1743893404000},
    {"coin": "BTC", "dir": "Open Short", "px": "67653", "sz": "0.00886",
     "fee": "0.086313", "closedPnl": "-0.086313", "time": 1743893404000},
    {"coin": "BTC", "dir": "Open Short", "px": "67653", "sz": "0.00262",
     "fee": "0.025524", "closedPnl": "-0.025524", "time": 1743893405000},
    {"coin": "BTC", "dir": "Close Short", "px": "67394", "sz": "0.01449",
     "fee": "0.140621", "closedPnl": "3.612289", "time": 1743895984000},
]


class TestCalcPnlFromFills:
    def test_winning_long_correct_net(self):
        """Trade 1: net should be sum(closedPnl) + funding, NOT minus fees again."""
        result = calc_pnl_from_fills(TRADE_1_FILLS)
        # sum(closedPnl) = -0.075914 + 2.919106 = 2.843192
        assert abs(result["net_pnl_usd"] - 2.843192) < 0.01

    def test_winning_long_gross_pnl(self):
        """Gross PnL should be pure price movement (before fees)."""
        result = calc_pnl_from_fills(TRADE_1_FILLS)
        # gross = sum(closedPnl) + sum(fees) = 2.843192 + 0.305018
        expected_gross = 2.843192 + 0.305018
        assert abs(result["pnl_usd"] - expected_gross) < 0.01

    def test_entry_exit_fee_split(self):
        """Entry and exit fees should be split correctly."""
        result = calc_pnl_from_fills(TRADE_1_FILLS)
        assert abs(result["entry_fee_usd"] - (-0.075914)) < 0.001
        assert abs(result["exit_fee_usd"] - (-0.229104)) < 0.001

    def test_losing_short_correct_net(self):
        """Trade 2: losing short."""
        result = calc_pnl_from_fills(TRADE_2_FILLS)
        # sum(closedPnl) = -0.141287 + (-1.74023) = -1.881517
        assert abs(result["net_pnl_usd"] - (-1.881517)) < 0.01

    def test_multi_fill_entry(self):
        """Trade 5: 5 open fills, 1 close fill."""
        result = calc_pnl_from_fills(TRADE_5_MULTI_FILLS)
        all_closed = sum(float(f["closedPnl"]) for f in TRADE_5_MULTI_FILLS)
        assert abs(result["net_pnl_usd"] - all_closed) < 0.01
        # Entry fees = sum of open fill fees
        open_fees = sum(float(f["fee"]) for f in TRADE_5_MULTI_FILLS
                        if f["dir"] in {"Open Long", "Open Short"})
        assert abs(result["entry_fee_usd"] - (-open_fees)) < 0.001

    def test_pnl_fields_sum_to_net(self):
        """net_pnl = pnl_usd + entry_fee_usd + exit_fee_usd + funding_usd."""
        result = calc_pnl_from_fills(TRADE_1_FILLS)
        computed = (result["pnl_usd"] + result["entry_fee_usd"]
                    + result["exit_fee_usd"] + result["funding_usd"])
        assert abs(computed - result["net_pnl_usd"]) < 0.001

    def test_with_funding(self):
        """Funding should be added to net PnL."""
        result = calc_pnl_from_fills(TRADE_1_FILLS, funding_usd=-0.8)
        expected_net = 2.843192 + (-0.8)
        assert abs(result["net_pnl_usd"] - expected_net) < 0.01
        assert abs(result["funding_usd"] - (-0.8)) < 0.001

    def test_empty_fills_returns_zeros(self):
        """No fills should return all zeros."""
        result = calc_pnl_from_fills([])
        assert result["net_pnl_usd"] == 0.0
        assert result["pnl_usd"] == 0.0

    def test_equity_return_pct(self):
        """Equity return pct should use provided equity."""
        result = calc_pnl_from_fills(TRADE_1_FILLS, equity=1000.0)
        expected = 2.843192 / 1000.0
        assert abs(result["equity_return_pct"] - expected) < 0.001
