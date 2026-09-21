# Shadow Risk Sizing

The deterministic sizing engine evaluates configured descriptive scenarios:
0.25%, 0.50%, 1.00%, and 2.00% of current account equity. It uses the exact
account snapshot and broker symbol metadata attached to each decision.

It calculates desired dollar risk, stop loss per lot, raw volume, broker-step
rounded volume, actual estimated risk, and rejection reasons. It never forces
minimum volume. Commission is not invented and live execution is unavailable.
