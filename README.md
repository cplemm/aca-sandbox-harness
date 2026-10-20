# ACA sandbox agent harness

A Python client that creates an **Azure Container Apps Sandbox**, bootstraps
Microsoft Agent Framework's real `create_harness_agent` runtime, and sends it
prompts. The agent runs Python/shell commands and uses files **inside the
sandbox**, with conversation history preserved for follow-ups.

This uses the dedicated **ACA Sandboxes** service, not Dynamic Sessions or
ordinary Container Apps. Communication uses SDK command/file operations:
there is no public HTTP server, container registry, or Foundry agent deployment.

```text
Python client + DefaultAzureCredential
    |
    | ACA Sandbox SDK: lifecycle, request files, fixed runner commands, results
    v
Sandbox: Python venv + Agent Framework harness + tools + session + workspace
    |
    | short-lived Entra bearer token supplied by the client for each prompt
    v
Existing Azure OpenAI deployment
```

## Prerequisites

- Python 3.12+ (the sandbox uses the public `python-3.12` disk image).
- [uv](https://docs.astral.sh/uv/) and Azure CLI for local development.
- An organizational Entra account, a supported sandbox region, and a sandbox group.
- An existing Azure OpenAI resource with a custom subdomain and a deployment
  supporting Chat Completions and function calling.
- **Container Apps SandboxGroup Data Owner** on the sandbox group for the client.
- **Cognitive Services OpenAI User** on the Azure OpenAI resource for that same
  client identity. Contributor/ARM permissions alone do not grant inference access.
- HTTPS access to the Microsoft package feed during bootstrap, and sandbox
  egress to the configured Azure OpenAI endpoint.

The setup helper can create a sandbox group separately; normal client operations
never create a parent group, modify role assignments, or deploy a model.

## Install and configure

All package resolution uses **https://packagefeedproxy.microsoft.io/pypi/simple**.
The project configures uv's default index; bootstrap passes the same feed
explicitly to pip, with hash-locked runtime dependencies.

```powershell
uv sync --locked --all-extras
Copy-Item .env.example .env
```

Edit `.env` with your subscription, resource group, sandbox group, supported
region, Azure OpenAI resource endpoint, and **existing deployment name**.
Use the resource root (`https://your-resource.openai.azure.com`), not an API path.
No model API key is needed.

```powershell
az login --tenant YOUR_TENANT_ID
uv run --env-file .env python examples\run_agent.py
```

The example creates a sandbox, generates/runs a Python script, reconnects for a
follow-up, downloads `analysis.txt`, and explicitly deletes the sandbox in
`finally`. Pass `--suspend` to stop it instead and print its ID for a later
reconnect. Downloads refuse to overwrite existing local files.
Local paths in these instructions use PowerShell syntax; paths inside the
Linux sandbox use POSIX syntax.

```powershell
uv run --env-file .env python examples\run_agent.py --suspend
```

For pip instead of uv, set `PIP_CONFIG_FILE` to the supplied `pip.ini` or pass
`--index-url https://packagefeedproxy.microsoft.io/pypi/simple` explicitly.
The `runtime` extra is only needed where the harness runs; the base install
supports the client without installing Agent Framework locally.

## Use from Python

```python
from azure.identity import DefaultAzureCredential
from aca_sandbox_harness import SandboxHarness, SandboxSettings

with DefaultAzureCredential() as credential:
    with SandboxHarness(SandboxSettings.from_env(), credential=credential) as client:
        sandbox = client.create()
        print(sandbox.sandbox_id)  # Save this ID if you will reconnect later.
        try:
            result = sandbox.run(
                "Write and run a Python script that computes 6 * 7. Save the answer to answer.txt."
            )
            if result.error:
                raise RuntimeError(f"{result.error.code}: {result.error.message}")
            print(result.text)

            sandbox = client.connect(sandbox.sandbox_id)
            followup = sandbox.run("Read answer.txt and explain the earlier calculation.")
            if followup.error:
                raise RuntimeError(followup.error.message)
            print(followup.text)
            sandbox.download_file("answer.txt", "answer.txt")
        finally:
            sandbox.delete()
```

Call `sandbox.suspend()` instead of `sandbox.delete()` to stop the sandbox and
retain its conversation and workspace for `client.connect(sandbox_id)` later.
Suspension waits until the sandbox is stopped; a failure is raised and does not
fall back to deletion.

`SandboxSettings` and `ModelSettings` can also be constructed directly.
`SandboxSettings.from_env()` reads environment variables; it does not itself
load `.env`. `uv run --env-file .env ...` loads that file.

| Method | Behavior |
| --- | --- |
| `client.create()` | Create and bootstrap once; return a sandbox handle |
| `client.connect(sandbox_id)` | Resume/check the existing installation without resetting state |
| `sandbox.run(prompt, timeout_seconds=300)` | Execute one bounded prompt; return a typed `RunResult` |
| `sandbox.suspend()` | Stop and retain the sandbox for a later reconnect |
| `sandbox.get_result(request_id)` | Retrieve a final result without executing again; `None` means none recorded |
| `sandbox.upload_file(local_path, remote_path)` | Upload into the workspace using a relative remote path |
| `sandbox.download_file(remote_path, local_path)` | Download a workspace artifact without overwriting |
| `sandbox.delete()` | Wait for deletion of this sandbox only; already deleted is harmless |
| `client.close()` | Release SDK resources, **not** delete sandboxes |

`RunResult` includes status, text, request/sandbox/conversation IDs, artifact
metadata, and an explicit error for failed runs. A succeeded status means the
harness completed its turn; inspect the answer and artifacts for task-specific
correctness. Agent text is not a substitute for verifying its work.

## Authentication and security

**This is a trusted-prompt, single-user prototype, not production credential isolation.**

The client retains `DefaultAzureCredential`. It authenticates to the sandbox
service using that credential, and separately requests an Azure OpenAI inference
token for `https://ai.azure.com/.default`. For each prompt, it forwards only the
short-lived token and its expiry, never the credential object, Azure CLI login
cache, refresh credentials, client secrets, or a model API key.

The token arrives via a mode-`0600` transient SDK-written file and is unlinked as
soon as the runner reads it. It is not put in shell arguments, normal result
objects, checkpoints, or tool subprocess environments. The client also attempts
cleanup in `finally`; cleanup failure is an explicit error. Literal token values
are redacted from command output and persisted conversation/result text.

**Arbitrary shell-capable code can still access credentials in the same sandbox,
including process memory.** Redaction and file permissions are hygiene, not a
containment boundary. A bearer token carries the client's actual inference
permissions until expiry, not permissions limited to this application's chosen
deployment. Do not submit untrusted tasks or use a broadly privileged identity.
For production, move model credentials behind an authenticated gateway or use
a separately scoped sandbox identity.

No credential refresh happens inside the sandbox. Each prompt needs a token
with at least its execution budget plus a two-minute margin remaining. If the
client's cached token is too close to expiry, the prompt fails before upload;
wait for refresh or reauthenticate rather than forwarding refresh credentials.

## Execution and lifecycle bounds

- One conversation and one active prompt per sandbox. Concurrent submissions
  receive an explicit `busy` result; use separate sandboxes for parallel work.
- Prompts default to **300 seconds**, with lower per-call limits supported.
  An async deadline and a Linux main-thread wall-clock alarm bound execution;
  process cleanup can add a short grace period. SDK network timeouts are
  separate from the agent execution budget.
- Shell commands default to **30 seconds**, with **16 KiB** retained per output
  stream. Output is drained but excess bytes are discarded and flagged.
  Linux command process groups are killed on timeout/cancellation, including
  background children.
- Function invocation is bounded to 16 model/tool rounds and 32 function calls
  per prompt. The framework may ask the model for a final summary at that limit.
- File transfer is limited to **16 MiB** per file; remote paths are normalized,
  relative to the workspace, and checked for symlink escapes under the
  conversation lock. Artifact listings contain at most 1,000 files and flag truncation.
- Session history is atomically checkpointed after a completed turn. A failed
  turn preserves the previous checkpoint, but shell/file side effects are
  **not rolled back**. Corrupt/incompatible checkpoints fail explicitly.
- Idle sandboxes auto-suspend after 10 minutes using disk mode. Operations
  check/resume them. There is no configured automatic deletion policy.
- Explicitly stopped sandboxes retain their disk state and stop compute billing;
  retained storage may continue to incur charges. Delete a sandbox when its
  conversation and workspace are no longer needed.
- Explicit deletion destroys sandbox conversation/workspace state. No volumes,
  user snapshots, or external state store are provisioned by this project.
  Closing Python is not cleanup: forgotten sandboxes and retained storage may
  still incur charges. Parent groups remain after sandbox deletion.

Control files live under `/tmp/aca-harness`; user artifacts live under
`/tmp/aca-harness/workspace`. These are Linux sandbox paths, not local paths.

## One-time sandbox group setup

The helper is **dry-run by default**, does not select a subscription, and
does not authenticate in dry-run mode:

```powershell
uv run python scripts\setup_sandbox_group.py `
  --subscription YOUR_SUBSCRIPTION_ID --resource-group YOUR_RG `
  --group YOUR_GROUP --region YOUR_REGION
```

After reviewing the target, run it with `--apply` to create a missing sandbox
group. It requires an existing resource group unless
`--create-resource-group` is explicitly supplied. It refuses to change an
existing group's region.

Role assignment is optional and requires additional permission to write role
assignments. Add `--grant-data-owner-to PRINCIPAL_OBJECT_ID` and the appropriate
`--principal-type User`, `Group`, or `ServicePrincipal` to explicitly grant
SandboxGroup Data Owner **on that group only**. The helper prints the principal
and scope before making changes. Assign Azure OpenAI inference access
separately on the existing model resource. Allow new role assignments to propagate.

## Failure recovery

- `BootstrapError` includes the sandbox ID. Bootstrap output is retained at
  `/tmp/aca-harness/bootstrap.log`; inspect it with the raw Sandbox SDK
  `get_sandbox_client(id).read_file(path)`. You can delete a failed installation
  through that raw client; `connect()` intentionally requires a valid installation.
- `ExecutionUncertain` includes the sandbox and request IDs. Reconnect and call
  `get_result(request_id)`. **Do not blindly rerun the prompt:** it may have
  already performed shell/file side effects. A missing result means unknown or
  still active, not successful cancellation.
- Requests with a `started` marker are not automatically replayed. Final
  results can be retrieved repeatedly without executing their tools again.
- ACA 401/403 errors point to the client identity/tenant and sandbox data-owner
  role; model 401/403 results point to inference access and the token audience.
- Runner protocol failures are reported without logging raw credentials or
  exception input values. Inspect the sandbox's request directory if diagnosis
  is needed. Never enable HTTP body logging or sensitive telemetry for token inputs.

## Development and validation

```powershell
uv sync --locked --all-extras
uv run pytest -q -m "not live"
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

Local coverage uses the real harness and OpenAI SDK with a fake HTTP transport,
plus SDK fakes and a fresh-process test of the uploaded runtime bundle.
The cloud smoke test is excluded unless deliberately selected:

```powershell
$env:ACA_HARNESS_LIVE_TEST = "1"
uv run --env-file .env pytest -q -s -m live
Remove-Item Env:ACA_HARNESS_LIVE_TEST
```

That test **creates and deletes a real sandbox** in an existing group and calls
the configured model. It does not create the group or deploy a model. Running it
requires a separate decision to use those Azure resources.

Versions available through the Microsoft feed are pinned in `uv.lock`; the
Sandbox SDK is `0.1.0b4`, Agent Framework core `1.21.0`, and OpenAI integration
`1.16.0`. The Sandbox API and harness file-access provider include preview or
experimental surfaces. Package feed availability may differ from upstream releases.
After dependency changes, regenerate the bootstrap requirements:

```powershell
uv export --extra runtime --no-dev --no-emit-project --no-header `
  --output-file src\aca_sandbox_harness\runtime-requirements.txt
```

References: [ACA Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview),
[Python Sandbox SDK](https://sandboxes.azure.com/docs/sandboxes/quickstart/setup-python-sdk),
[Agent Harness](https://learn.microsoft.com/agent-framework/concepts/harness?pivots=programming-language-python),
[Azure OpenAI Entra authentication](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/managed-identity).
