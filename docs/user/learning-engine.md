# Learning engine

A small constrained bandit chooses, per decision point and context bucket (context fill, tool
category, output size, content type, task phase), how strongly to trim, under a hard quality
constraint: it never trades quality for tokens in a weighted sum. Each trim is watched for signs
it hurt (the agent reading the saved full output, re-running the command, asking for trimmed
content); a trimming level is used only while its plausible regret stays under a target (5 % for
compression). A new project starts with the lightest level and unlocks stronger ones one at a time
only after the previous one has been observed at least 20 times with a low regret estimate
(experience from your other projects counts toward that, so a new project may unlock stronger
levels sooner), so early sessions trim less than later ones. A share of decisions always keeps the output untouched (roughly one in five at first on a new project), as a baseline. The
subagent brief stays off (it is not learned until a benchmark shows it does not hurt quality).
`[learn] enabled = false` switches learning off, but that is not a way to trim less: it uses fixed
defaults (standard compression, read de-dupe pointers), which trim more than a cold learner. To
trim nothing, use the hold in [Turning it off](host-guides/claude-code.md#turning-it-off). A project's `token-sparen.toml`
can only make learning more careful (see [What a project file may change](configuration.md)); it
cannot turn learning off, so a project that wants less trimming sets
`[learn.hold] compression = true`. When a session ends, a background `token-sparen fold` merges what the session learned into the
project (and, weighted 0.3, into a cross-project global store that forgets after 90 idle days);
sessions it missed are folded at the next session start. A circuit breaker pauses a decision point
for the project for 3 sessions when the learner's recent trims clearly caused re-reads at more than
twice the target rate, and tells you once. The starting values of the learner's priors are
hand-set for now.
