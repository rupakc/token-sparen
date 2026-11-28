# Tool pruning (opt-in)

MCP servers you have configured but never use in a project still put their tool names and
server instructions into every request. Tool pruning removes such **unused MCP servers** from the
model's context in that project. The saving is modest (those servers' tool names and
instructions, on every request); token-sparen cannot measure it from hooks, so
the statusline (`⊘ N`) and `token-sparen stats` show only the number of pruned tools. Built-in
tools are left alone by default: in interactive Claude Code they are deferred and cost a few
tokens each. It **writes the checkout's `.claude/settings.local.json`**, so it is off until you
opt in:

```
token-sparen prune enable     # opt this project in (or [prune] enabled = true in ~/.token-sparen/config.toml)
token-sparen prune status     # state, pruned tools, candidates and their evidence
token-sparen prune restore mcp__github   # give one server back now (it is never pruned again)
token-sparen prune restore --all         # remove every rule token-sparen wrote (exact revert)
token-sparen prune disable    # opt out; also restores everything, in every checkout
```

Only `prune enable` or `[prune] enabled = true` in your own `~/.token-sparen/config.toml` turn it
on: `TOKEN_SPAREN_PRUNE__ENABLED` is ignored (a repository's committed Claude Code settings can
set environment variables), and a project's `token-sparen.toml` can only switch it off
(`[prune] allow = false`, which also reverts token-sparen's rules). `[prune] builtins = true` in
your `config.toml` also prunes built-in tools, but only those an SDK `init` record listed as always
loaded.

What it does: enabling first reads the project's earlier Claude Code transcripts (last 60 days, at
most 50 MB) so that nothing used before is ever pruned, and says how many it found. Nothing is
pruned until such a read has succeeded: if none were found, the first recorded session's folder
is read instead. `prune` commands find the project from `--project`, else `CLAUDE_PROJECT_DIR`,
else the git top level of the current directory; `prune restore <tool>` restores it wherever
token-sparen pruned it in this project (also in another checkout). When a session is folded, token-sparen
reads the tools the session had (from the transcript's tool list where Claude Code records one; a
session without one counts for the tools other sessions listed) and the tools it used, including
in subagents. A **whole MCP server** none of whose tools was ever used that stayed unused across
at least 3 sessions and 15 prompts becomes a candidate; it is pruned only once a deliberately
strict regret bound clears (about 300 unused prompts with the defaults). Never pruned: `Read`,
`Edit`, `Write`, `Bash`, `Grep`, `Glob`, `Agent`/`Task*`, `TodoWrite`, `AskUserQuestion`,
`Skill`, `ToolSearch`, plan mode, `SendMessage`, and any tool ever used in the project. Pruning
adds a bare `permissions.deny` entry such as `"mcp__server"`, which removes the server's tools
from the model's context. Because that changes the prompt, new rules are written only by the
background fold that runs when a session ends, and only when no other session of the project
(one that has not ended) was active in the last 60 minutes; otherwise they wait for a later
session end. They take effect from the next session; a session start never writes them. A
pruned server collects no evidence while it is pruned (its "unused" would be caused by the
pruning itself), so each prune is a **lease**: after 15 sessions (`[prune] lease_sessions`; a
project may only lower it) token-sparen restores it for at least one session to see whether it
is used, and prunes it again only after fresh unused sessions.

What it writes: only those deny entries, appended to `permissions.deny`; every other key and
every rule you wrote stays as it is, and a rule you already had is never claimed. Before the
first change the file is copied once to `<project>/.token-sparen/settings.local.json.token-sparen.bak`
(inside the git-ignored `.token-sparen/` folder, so the copy of your local settings is never an
untracked file a `git add -A` would commit; a backup an older version left next to the file is
moved there); writes are atomic and re-check the file just before replacing it. The file is
written only when git confirms it is untracked and covered by `.gitignore`, and that the backup
location is ignored too; a file that is tracked, not ignored, outside a git work
tree, or whose status git cannot report (git missing, failing or too slow, or `GIT_DIR`-style
variables pointing elsewhere) is never written, nor a symbolic link (or a file inside a linked
`.claude` folder), nor one that is not a strict JSON object (comments, duplicate keys); you are
told why (token-sparen does not edit `.gitignore`). The
backup is refreshed when token-sparen starts a new round of rules in a file where it owns none,
so it never predates your own edits. Sibling clones and worktrees of one repository share what
was learned, but each checkout's file and rules are tracked separately. A `prune enable` belongs
to the project as identified then: if its git remote changes, the next fold restores the tools
pruned under the old identity and tells you (`prune enable` opts the project in again).

Recovery: each session start shows one line, `Pruned (unused here): A, B — run \`... prune
restore <tool>\` if needed.`, to you and to the model (with the plugin's own launcher path when
`token-sparen` is not on your PATH; the `/token-sparen` skill pre-approves `prune restore`). If the
agent searches for a pruned tool with ToolSearch, tries to call it, or you name it in a prompt
(its exact name such as `mcp__github`, or a multi-word form such as `claude ai gmail`; a single
common word like "github" does not count), token-sparen removes its rule immediately and tells
you that it is no longer denied (Claude Code may need a new session to show it again); it is
never pruned again. A rule you delete yourself
counts the same way. `restore --all` and `disable` remove exactly the rules token-sparen wrote, in
every checkout, and the file (and the `.claude` folder) again if token-sparen created them.
