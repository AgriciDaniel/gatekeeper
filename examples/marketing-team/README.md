# Example: a marketing team of Markdown skills

`team.json` lists 24 skills, each owned by a role (strategy, research, seo, content, ads, social, email, creative, analytics, web, workflow). The assistant does not launch a subagent per role. It reads the chosen skill's `SKILL.md` and follows it, so this gate runs in `advise` mode: it adds the route as context and never blocks.

```bash
bin/gatekeeper roster marketing-team
bin/gatekeeper judge  marketing-team "set up a linkedin ads campaign for cfos"
bin/gatekeeper bench  marketing-team        # 20 labelled prompts, about $0.001
```

How it decides, in order:

1. Not marketing (code, git, conversation): allow, no suggestion.
2. One skill in the act band (0.80 and up): route to it and name its `path`.
3. No confident skill, but one role is (`rollup: group`): suggest the role.
4. A skill in the confirm band: suggest it.
5. Otherwise: ask the user what outcome they want.

To use this pattern with your own library, point `roster.index.file` at your index and map its field names with `name`, `description`, `group` and `path`. The skills and descriptions here are written for this example; the `skills/` paths are placeholders.

The bench prompts are deliberately clear-cut, so 20 of 20 shows the mechanics, not real-world accuracy. Label prompts from your own log before trusting the bands.
