# Template-specific protocol reprompts

Host-parsed agent prompt templates share one **expected output shape** fragment between the first prompt and the protocol reprompt sent after `AgentOutputProtocolError`. Pycastle keeps malformed output malformed, instead of broadening parsers to accept provider formatting drift such as JSON-string-escaped `<plan>` bodies, because the clearer boundary is to make each correction prompt name the exact required host-parsed form.

## Considered Options

- **Accept escaped Planner JSON.** Rejected: it fixes the immediate OpenCode Planner incident by weakening the `<plan>` contract and leaves other protocol roles with the same generic retry problem.
- **Keep one generic protocol reprompt.** Rejected: the observed Planner failure repeated the same malformed output because the retry prompt did not restate the expected raw JSON object shape.
- **Own expected output shapes by role only.** Rejected: some roles, notably Improve, use multiple prompt phases with different required host-parsed output forms.

## Consequences

- Protocol reprompts are prompt-template-specific and reuse the same expected-output-shape fragment as the original prompt.
- Parser tolerance remains intentionally narrow; malformed required tags still raise `AgentOutputProtocolError`.
- Existing initial prompt layout stays intact; expected-output-shape text is extracted into fragments, reinjected into the same prompt position, and reused for reprompts.
