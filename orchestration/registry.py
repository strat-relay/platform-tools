from __future__ import annotations

from typing import Any


class StrategyRegistry:
    def __init__(self, config: dict[str, Any]):
        self.records = {x["strategy_id"]: x for x in config.get("strategies", [])}

    def enabled(self) -> list[dict[str, Any]]:
        return [x for x in self.records.values() if x.get("enabled")]

    def get(self, strategy_id: str) -> dict[str, Any] | None:
        return self.records.get(strategy_id)


class PortfolioRegistry:
    def __init__(self, config: dict[str, Any]):
        self.portfolios = {x["portfolio_id"]: x for x in config.get("portfolios", [])}
        self.accounts = {x["account_id"]: x for x in config.get("accounts", [])}

    def routes_for(self, strategy_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        result = []
        for portfolio in self.portfolios.values():
            if not portfolio.get("enabled") or strategy_id not in portfolio.get("strategy_ids", []):
                continue
            for account_id in portfolio.get("account_ids", []):
                account = self.accounts.get(account_id)
                if account and account.get("enabled"):
                    result.append((portfolio, account))
        return result
