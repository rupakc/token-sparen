# Budget governor

**Hooks:** UserPromptSubmit, PostToolUse, and PreToolUse for the opt-in hard stop. **Default:** on, advisory only. **Learned:** no.

The governor tracks the billed context tokens of your session and the share of the context window used by the last call, and tells you in the terminal (no model tokens):

- budget notices when the session passes 50 % and 80 % of the configured token budget (default 500M billed context tokens, `governor.session_budget_tokens`);
- context-window notices at 60 % and 80 %.

It never compacts for you.

## Hard stop (opt-in, off by default)

With `governor.hard_stop = true`, once the session passes 100 % of `session_budget_tokens`, context-adding tools (Read, Bash, WebFetch, WebSearch, Grep, Glob, LS, Task/Agent, MCP tools) are denied. Edit tools are never blocked. A project file cannot set `governor.hard_stop`.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `governor.enabled` | true | budget and context notices (a project may only turn it off) |
| `governor.session_budget_tokens` | 500000000 | economy budget the notices and hard stop use |
| `governor.hard_stop` | false | deny context-adding tools at 100 % of the budget |

See [Configuration](../configuration.md) and the [statusline](statusline.md) `burn` and `ctx` segments.
