# Portfolio and Account Model

Routing is `strategy -> portfolio -> account`, not strategy directly to an
account. The initial configuration has one redacted Exness DEMO shadow
account and one Context shadow portfolio. Account references are descriptive
only and contain no credentials.

Execution modes are represented in configuration, but this implementation
allows only `SHADOW`. `LIVE_ARMED` is not accepted by the runtime.
