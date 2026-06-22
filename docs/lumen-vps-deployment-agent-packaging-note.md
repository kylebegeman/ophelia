# Deployment Agent Packaging Note

Status: public-safe summary

Deployment agents should call an installed `ship` console script instead of
depending on a maintainer's local checkout. Package resources, templates, and
command metadata should come from the installed Ophelia package.

Expected integration shape:

- install Ophelia into an isolated Python environment
- invoke `ship` through the environment's console script
- pass manifest paths and runtime roots explicitly
- consume JSON with `--json`
- preserve dry-run-first mutation gates and confirmation tokens
- keep runtime state, env files, receipts, and provider evidence outside Git
