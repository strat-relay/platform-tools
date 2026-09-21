# Live arming and kill-switch design

Live arming is fail-closed and unavailable in Phase 1. A future live phase
must require all of: `LIVE_ARMED`, explicit account/strategy/portfolio
allowlists, a valid risk policy, broker identity match, an environment
confirmation, startup write audit, and an operator arm record.

The configuration schema includes global, account, strategy, and portfolio
disarms plus optional daily loss, equity-loss, simultaneous-risk,
strategy-risk, and open-position limits. All are unset/disabled for dry run;
no numeric limits are invented here.
