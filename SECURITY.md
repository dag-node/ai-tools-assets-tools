# Security policy

The tools here run in a publisher's CI and on a maintainer's machine,
and decide what a set may carry. A validator reads set content as data: it does
not import Python from a set, execute a set's scripts, run its build or source
its configuration, so a set under check cannot run code through the check.

## What counts

A vulnerability here is a check that lets a set carry what the format refuses,
a tool that executes content it should read, a build that stages a file
the inventory does not cover, or a workflow that exposes a release secret
to a job a pull request controls. Include the tool, its version or commit,
the input, and what it accepted or ran.

## Supported versions

Only the latest release is supported for security updates. A publisher
repository pins a release in `tools.pin` and moves it on purpose.

## Reporting a vulnerability

Please report suspected vulnerabilities privately:

* [GitHub private vulnerability
  reporting](https://github.com/dag-node/ai-tools-assets-tools/security/advisories/new).
* Alternatively, email **[tools@dagnode.com](mailto:tools@dagnode.com)**
  with the subject prefix `[SECURITY]`.

Do not include vulnerability details in public issues or pull requests.

## Handling and disclosure

Security reports are reviewed and prioritized according to their potential
impact. Response and remediation times depend on maintainer availability; no
fixed timeframes are guaranteed.

Please coordinate public disclosure with the maintainer so that users can
receive a fix or mitigation where possible.
