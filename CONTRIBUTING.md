# Contributing

This project welcomes contributions and suggestions. Most contributions require you to
agree to a Contributor License Agreement (CLA) declaring that you have the right to,
and actually do, grant us the rights to use your contribution. For details, visit
https://cla.microsoft.com.

When you submit a pull request, a CLA-bot will automatically determine whether you need
to provide a CLA and decorate the PR appropriately (e.g., label, comment). Simply follow the
instructions provided by the bot. You will only need to do this once across all repositories using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/)
or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Development

The base library supports Python 3.11 or later. Use Python 3.13 or later for
development with all optional integrations: the agents runtime requires 3.13.
From the repository root in a virtual environment, install the dependencies
and run the tests:

```bash
python -m pip install -e ".[dev,functions,identity,hosted-skills]"
python -m pytest
```

Installing all extras allows the tests to exercise optional integrations rather
than skip them. Follow the existing code style and include focused tests for
behavior changes.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening
a public issue.

## Package builds and releases

In a Python 3.13+ virtual environment, run the credential-free build from
PowerShell:

```powershell
.\scripts\build-pypi.ps1
```

This reuses the existing release test suite and all optional integrations,
checks that the versions in `pyproject.toml` and
`src\app_service_extensions\__init__.py` agree, replaces `dist`, builds a
wheel and source distribution with the configured Poetry backend, and runs
strict Twine metadata checks. A failed test or check prevents artifact publication.
The legacy `scripts\publish-pypi.ps1` delegates to this same build script;
its direct Twine upload is not the Microsoft production release path.

### Azure DevOps mirror setup

The GitHub and Azure Repos mirrors use the same build script and tests.
The YAML files are drafts until the ADO resources below are configured.
No pipeline, identity, certificate, or service connection is created by these files.

Both pipelines extend `v2/OneBranch.Official.CrossPlat.yml` from
`OneBranch.Pipelines/GovernedTemplates`, following the EasyAuth official
pipeline's template reference, Windows 2022 container, and managed Windows pool
declaration. No team-specific agent queue is hardcoded. SDL checks remain enabled;
TSA upload is disabled as in the EasyAuth example, with policy checks breaking
the build. The build reuses `scripts\build-pypi.ps1`; OneBranch publishes
`python-build-outputs` from `ob_outputDirectory`, with distributions under `dist`
and governance metadata outside that subfolder.

The release uses OneBranch's `NonAzure` category with
`ob_release_environment: Production` and a managed Windows release pool.
OneBranch creates the deployment job targeting `pypi-release`, injects its
release compliance/approval jobs, and downloads the declared pipeline artifact
with production artifact validation. Do not copy the EasyAuth NuGet signing,
publishing, or Docker stages: they are unrelated to this Python package.

ESRP onboarding is already complete. The reusable
`.pipelines/templates/publish-pypi-esrp.yml` template uses the existing
`antares-custommiddleware-prod-esrp` Azure Resource Manager service connection
in the Antares project, authenticated through workload identity federation,
and its client ID `486d42bd-c87d-4915-8fc5-9b1174954c82`. The request-signing
certificate is `esrp-requestsigning-cert` in the existing
`custommiddleware-prod-kv` Key Vault.
The ESRP Release v14 task is installed in the ADO organization. The template
uses that version and the connection's AME tenant
`33e01921-4d64-4f8c-a055-5bdaffd5e33d`. Do not create a replacement identity,
service connection, or certificate.

1. Create a build pipeline named
   `app-service-functions-custom-middleware-build` pointing at
   `.pipelines/build-pypi.yml` in the mirrored repo. It builds `main` and publishes
   `python-build-outputs` only after successful tests and distribution checks.
   Add Azure Repos branch-policy build validation if desired; `pr: none` does
   not disable Azure Repos branch policies. The build requires no release secrets.
   Authorize access to `OneBranch.Pipelines/GovernedTemplates` and complete the
   pipeline's OneBranch official-build onboarding, including managed-pool access.
   The build container must support the `UsePythonVersion@0` download of Python
   3.13 and PowerShell 7.3+ for the release-preflight tests.
2. Use the internal Antares project for release and authorize the existing
   service connection only for the release pipeline, never public/PR builds.
   Create the `pypi-release` ADO environment **before running the pipeline**.
   Configure required human approvals and branch-control checks for `main`
   (including the build pipeline resource). Restrict environment, service
   connection, pipeline editing, and queue permissions to release maintainers.
   YAML alone does not establish these approval/security boundaries.
   Configure approvers to prevent self-approval. Complete OneBranch production
   release onboarding and the applicable Service Tree/approval-service policy
   configuration; existing ESRP onboarding alone does not provision this.
3. Create a separate release pipeline pointing at `.pipelines/release-pypi.yml`.
   The existing service connection, client ID, Key Vault, and request-signing
   certificate are configured.
   Supply the exact `releaseVersion` from package metadata, plus `esrpOwners`
   and `esrpApprovers` when queuing a release. Contacts are
   comma-separated individual `alias@microsoft.com` accounts, not distribution
   lists or security groups. Owners and approvers must be disjoint; the template
   validates this and normalizes the lists before invoking ESRP. Choose the role
   split explicitly rather than putting the same people in both lists.
   Do not put certificates, private keys, or PyPI tokens in YAML.
   If the build pipeline is in another project, add its `project` under the
   pipeline resource and grant build/artifact-read permission to the release
   pipeline's Build Service identity. The release uses `System.AccessToken`
   only to verify the selected build's successful result and commit via the
   ADO Build API; it is not an ESRP or PyPI credential.

### Package-feed authentication

Feed authentication concerns **downloading build dependencies**, not publishing
to PyPI. The build installs `build`, `twine`, Poetry's build backend, pytest, and
all optional integrations. Public PyPI does not require credentials for these
downloads. No private feed has been specified for this repo, so the build does
not add `PipAuthenticate@1` or assume the sister team's feed is appropriate.
The agent's existing pip configuration still applies.

If Antares requires an Azure Artifacts feed with a PyPI upstream, add
`PipAuthenticate@1` immediately before the shared build-script step, in the same
job. For example, replacing `PROJECT/FEED` with the approved project-scoped feed:

```yaml
- task: PipAuthenticate@1
  inputs:
    artifactFeeds: PROJECT/FEED
    onlyAddExtraIndex: false
```

The task supplies authenticated pip index environment variables to subsequent
steps. The script's pip installs and isolated `python -m build` subprocesses
inherit them; do not hardcode a PAT or print the index URLs. Grant the pipeline's
Build Service identity feed Reader access for existing packages, or **Feed and
Upstream Reader (Collaborator)** if it must save new packages from the PyPI
upstream. A feed in another project also needs the appropriate project access.
An external-organization feed requires a separately authorized feed connection,
not the ESRP Azure Resource Manager service connection.

Prefer one approved primary feed with the required upstream over combining
public and private indexes. `onlyAddExtraIndex: true` leaves the existing primary
index and adds feeds as extras; pip considers candidates across indexes rather
than treating the private feed as a reliable fallback. Feed authentication does
not configure proxy/TLS access, pin dependency versions, or authenticate ESRP.
The release job installs no Python dependencies and needs no pip-feed credentials;
its ESRP connection handles publishing separately.

### Releasing a version

Update both package versions and mirror the reviewed commit to ADO `main`.
After the build succeeds, manually queue the release pipeline from `main` and
enter the exact intended `releaseVersion` (for example, `1.0.0`).
Under **Run pipeline > Resources**, select that exact successful build and
verify its commit and artifacts before approving the environment deployment.
The resource defaults to the latest successful `main` build if not explicitly
selected. Both release triggers and PR triggers are disabled. The release rejects
non-`main` runs and checks the actual wheel `METADATA` and sdist `PKG-INFO` Name
and Version headers against the fixed package name and requested version before
ESRP can run. Archives are inspected without extraction or executing package code.

The release downloads the selected wheel and source distribution without
checking out source, installing dependencies, rebuilding, or invoking Twine
upload. ESRP packages the folder, submits it for release, and waits for completion;
no separate release JSON or ZIP-building script is needed for the Folder input.
Check the ESRP result and the package's PyPI page to confirm the published
version. If ESRP succeeds but nothing appears, ask the ESRP team to verify the
client ID's public-PyPI configuration. PyPI filenames cannot be reused for changed
artifacts; bump the version rather than overwriting a published release.

References (Microsoft internal access required):

- [Publishing to PyPI via ESRP Release](https://eng.ms/docs/microsoft-security/identity/trust-and-security-services/tss-release-distribute/tss-release-esrp-parent/oss-publishing/releasing-open-source/pypi.html)
- [Guide for Integrating with the ESRP Release ADO Task](https://eng.ms/docs/microsoft-security/identity/trust-and-security-services/tss-release-distribute/tss-release-esrp-parent/manage-and-publish-releases/release-ado-task-integration/adotaskintegration.html#high-level-steps)
- [EasyAuth official OneBranch pipeline](https://msazure.visualstudio.com/One/_git/AAPT-Antares-EasyAuth?path=/.pipelines/OneBranch.Official.yml&version=GBdev)
- [PipAuthenticate task reference](https://learn.microsoft.com/en-us/azure/devops/pipelines/tasks/reference/pip-authenticate-v1?view=azure-pipelines)

The reusable step layout follows the sister team's
[ESRP PyPI template](https://github.com/Azure/azure-functions-python-extensions/blob/dev/eng/templates/official/steps/publish-pypi-esrp.yml).
This repo uses its own service connection, vault, certificate, and client ID,
and the installed v14 task's `PyPi` input value.
