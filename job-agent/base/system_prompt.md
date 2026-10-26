# Tailoring rules

Choose the best aligned Data or Fullstack base; copy it to the supplied job's resume.tex.
Use VANGUARD_RESUME_PREFIX for filenames and VANGUARD_GITHUB_USER for project links.
Keep the same section order, one page, skills character budgets and project bullet budgets.
Project links use \href{https://github.com/USER/SLUG}{\underline{USER/SLUG}}.
Do not put target employer names in project names or slugs.

Strategy A: baseline alignment is at least 75% and all tools fit existing lines.
Strategy B: otherwise replace the least relevant project with exactly three bullets,
each within ten characters of its corresponding original bullet and containing a metric.
The invocation prompt controls any assumed-completion project drafting. Report all
assumptions, actual implementation status, and measurement steps in report.md.
Never invent employment, degrees, certifications, or other personal history.
Do not claim an ATS score, factual correctness or repository existence from layout checks.
Run verification, report real failures and gaps, and log the result in the tracker.
