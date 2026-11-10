# Workflow security policy

Pull requests, manual test runs, and release callers run the full test matrix and the workflow policy tests. This includes documentation-only and workflow-only changes. There is no change scanner that can skip required tests. The stable `final_check` job succeeds only when both prerequisites succeed; failures, cancellation, and unexpected skips fail the check.

Release and CodeQL jobs require `github.repository == 'Nike-Inc/koheesio'`. Fork pull requests, branch pushes, tag pushes, and scheduled runs cannot enter those jobs. The Release build also guards the artifact upload step. CodeQL skips its entire job in forks, including init/analyze post steps; database uploads are disabled upstream too.

Release calls `./.github/workflows/test.yml` from the same commit. Both test and build checkouts use `github.sha`, and the artifact name contains that SHA. Building requires the reusable test workflow to succeed. Publishing requires both tests and the build, an upstream push, and a `koheesio-v*` tag. The publisher downloads only the artifact from its current workflow run. It retains the `release` environment and OIDC `id-token: write`; build and test jobs have only `contents: read`. Checkout credentials are not persisted.

The former automatic Dependabot merge workflow has been removed. Dependabot proposes GitHub Actions and Python updates for review. Reviewers must resolve each proposed action ref in its owning repository, verify the action's exact subdirectory, update `workflow-policy/action-pins.json`, and review the resulting changes before merging. The manifest records reviewed commit and action-file identities; it is not a claim that a tag cannot move or that a commit signature establishes safety. SHA pins fix the referenced action source, but downloaded toolchains and dependencies have their own update and supply-chain boundaries.

Run the local policy tests with Python and PyYAML installed:

```sh
python -m unittest discover -s .github/workflow-policy -p 'test_*.py' -v
actionlint .github/workflows/*.yml
```

The policy tests parse the checked-in workflows, evaluate the supported guard expressions over fork/upstream event scenarios, execute the final-check shell for every prerequisite outcome, and check permissions, action identities, and the release dependency graph. These are local source checks. They do not establish hosted runner behavior, a passing Python/Spark matrix, release-environment approvals, organization Actions policy, or PyPI trusted-publisher configuration. Those controls and a hosted release remain unverified until separately authorized and observed. No private upstream security-reporting channel is documented here.
