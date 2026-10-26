# Contributing

Start with [setup](docs/setup.md) and [how it works](docs/how-it-works.md).
The stable path is scrape → resume → verify → export. The auto-applier has its
own [development guide](docs/auto-applier.md) and stays outside the daily runner.

Create a branch, make a focused change, and include reproduction steps, resulting
behavior and validation in the pull request. Prefer offline fixtures over real account
traffic. Add regression coverage when changing orchestration, persistence, export or
submission behavior. Do not change someone's resume bases or copy private artifacts
into a fixture. Use fabricated applicant facts, links and credentials.

Run the three offline suites documented in the auto-applier guide. CI also runs the
local browser fixtures. Resume compile tests require TeX and Poppler and use fictional
example bases; they never use your private resumes. Keep pipeline configuration
portable, maintain component CLIs, and document changes to setup or behavior.
