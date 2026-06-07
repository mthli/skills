Use a workflow to run these five skills in parallel:

- /base-breakout-scan
- /mean-reversion-scan
- /momentum-scan
- /regime-scan
- /unusual-options-scan

Each writing its output files only (no git).

After all of them finish, check git status;
if there are changes, commit them on the current branch and push,
otherwise do nothing.
