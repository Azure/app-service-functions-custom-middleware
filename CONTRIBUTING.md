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
Twine is used only for metadata validation; publishing goes through the ESRP
release pipeline. There is no direct-upload script or local PyPI-token workflow.

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
