You are keeping README.md of the Earr repository in step with the code.

The commits ${BEFORE}..${AFTER} were just pushed to main. Look at them with
`git log ${BEFORE}..${AFTER}` and `git diff ${BEFORE}..${AFTER}`, then read
README.md and decide whether any of its prose no longer matches what the app
does.

Update README.md only for changes a user or admin of Earr would notice: a new
or removed feature, a setting that was added, renamed or moved, changed
defaults or behaviour, a new integration, or setup steps that are no longer
right. Internal refactors, tests, CI and dependency bumps are not README
material. If nothing user-visible changed, change nothing and say so.

Rules:

- Edit README.md only. Do not create or change any other file.
- Never edit anything between `<!-- BEGIN GENERATED: ... -->` and
  `<!-- END GENERATED: ... -->`. Those tables are rebuilt from the code
  afterwards, so edits there are thrown away.
- Never add, remove or change image references or the `<picture>` elements.
  Screenshots are taken separately.
- Keep the existing structure, headings and tone: plain, direct sentences,
  setting names in bold exactly as the UI labels them, paths like
  `Settings > Library`. Add a section only when a whole new feature needs one.
- Describe what the code actually does. When unsure, read the code; do not
  guess.
- Keep edits as small as the change calls for.

When you are done, reply with one or two sentences saying what you changed and
why, or that nothing needed changing.
