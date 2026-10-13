# Docker-only CI (activation required)

`ci/github-actions.yml` is the ready-to-activate workflow. It builds the
production Dockerfile for Python 3.11 and 3.12 and runs Ruff, format checking,
and the full pytest suite **inside containers**. No production credentials,
`.env`, external Compose networks, or host Python environment are required.

The current GitHub App cannot publish `.github/workflows/*`: GitHub rejected
the push because the App lacks `workflows` permission. No active workflow is
present on the reviewed branches. An old workflow record in Actions is not
evidence that current PRs are being tested.

A maintainer with workflow permission should activate this on the existing
working branch (do not create another branch):

```sh
mkdir -p .github/workflows
cp ci/github-actions.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml
git commit -m "ci: activate Docker validation"
git push origin arena/6d24f54e-discord-radio
```

Then verify the PR's Python 3.11 and 3.12 Docker jobs pass and make them required
checks through repository settings if desired. Until activation, `make test`
and `make lint` are mandatory manual Docker release gates. After activation,
keep the reference and active workflow synchronized, or remove the reference.
