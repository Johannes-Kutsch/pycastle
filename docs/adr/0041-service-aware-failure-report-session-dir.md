# Service-aware Failure-Report session dir

Failure-Report prompts receive `SESSION_DIR` so the diagnostic agent can inspect the failed agent's preserved session state. That path must point at the state directory for the service that actually failed.

This matters now that the default planner chain starts with OpenCode. A planner `protocol_error` from OpenCode creates `.pycastle-session/planner/opencode/`, but the old `AgentFailedError.session_dir` property always returned `.pycastle-session/<role>/claude`. The Failure-Report agent was therefore sent to a missing Claude path and filed a weaker issue with no transcript context.

`AgentFailedError` now carries the failed service name from `RunRequest.service`. Its `session_dir` property formats `.pycastle-session/<role>/[<namespace>/]<service>`, preserving the old Claude path only as the default for legacy direct construction in tests and helper code.

Alternatives considered:

- Recompute the failed service in `run_iteration` from the stage override. Rejected because fallback dispatch means the configured first choice is not always the service that failed.
- Scan `.pycastle-session/<role>/` and pick the only service child. Rejected because multiple service dirs can exist after fallback or prior attempts, and an empty dir can still be the correct failure artifact.
- Pass the whole agent log file path instead of `SESSION_DIR`. Useful as a future improvement, but it does not replace the service state path used by resume cleanup and transcript inspection.
