# Concepts

**Hook.** Claude Code runs token-sparen's commands at fixed moments (session start, before and after a tool call, before compaction, when a prompt is submitted, at session end). token-sparen reads the event and answers "no change" or one small action. See [Architecture](architecture.md).

**Decision point.** A place where token-sparen chooses between arms (for example how strongly to trim an output, or whether to return a read pointer). Decision points are what the [learning engine](learning-engine.md) steers.

**Arm.** One option at a decision point. Compression has three trimming levels (light, standard, aggressive) and an untouched arm.

**Mechanism.** The code behind one behaviour: [compression](mechanisms/compression.md), [guards](mechanisms/guards.md), [read de-duplication](mechanisms/read-dedupe.md), [governor](mechanisms/governor.md), [boundary](mechanisms/boundary.md), [subagent brief](mechanisms/subagent.md), [facts](mechanisms/facts.md), [tool pruning](mechanisms/tool-pruning.md), [loop guard](mechanisms/loop-guard.md) and the [statusline](mechanisms/statusline.md).

**Marker and recovery.** Every trim ends with `[token-sparen: kept X of Y lines · full output: <path> · ...]` and the full text is on disk, so nothing is lost ([Recovering trimmed output](host-guides/claude-code.md#recovering-trimmed-output)).

**Regret.** A sign that a trim hurt: the agent read the saved full output, re-ran the command, or asked for trimmed content. The learner uses a trimming level only while its plausible regret stays under a target.

**Net savings.** Tokens removed by trims, minus the tokens the agent spent re-reading trimmed output, minus the tokens token-sparen itself injected. `/token-sparen` and the statusline use the same net figure; it can be zero or negative.

**Fail open.** Any error, or a hook slower than 400 ms, leaves everything unchanged and logs one line.

**Project, state root.** A project is identified by a hash of its normalised git remote (else its directory). Learning state lives under `~/.token-sparen/`; recovery files live in `<project>/.token-sparen/`. See [Privacy and security](privacy-security.md).

**Quality first.** Quality is a hard constraint, never traded in a weighted sum: file contents are not trimmed by default, only a short list of content types is offered for trimming, and a project file can only make token-sparen intervene less ([Configuration](configuration.md)).
