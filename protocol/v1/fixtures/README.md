# Bridge protocol fixtures

These fixtures are source-controlled characterization inputs for both future
repositories. They describe the current MCP protocol/profile surface only;
they are not an implementation module and do not authorize broker operations.

During extraction, the bridge and platform client test suites must consume the
same fixture files. Any intentional wire change requires a protocol-version
bump and an explicit migration decision.
