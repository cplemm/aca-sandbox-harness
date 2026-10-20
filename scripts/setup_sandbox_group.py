"""Opt-in parent-group setup. Never run automatically from the client."""

from __future__ import annotations

import argparse
from uuid import UUID, uuid5

from azure.containerapps.sandbox import SandboxGroupManagementClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization.v2022_04_01 import AuthorizationManagementClient
from azure.mgmt.authorization.v2022_04_01.models import RoleAssignmentCreateParameters
from azure.mgmt.resource.resources.v2025_04_01 import ResourceManagementClient
from azure.mgmt.resource.resources.v2025_04_01.models import ResourceGroup

DATA_OWNER_ROLE = "c24cf47c-5077-412d-a19c-45202126392c"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--create-resource-group", action="store_true")
    parser.add_argument("--grant-data-owner-to", metavar="PRINCIPAL_OBJECT_ID")
    parser.add_argument(
        "--principal-type", choices=["User", "Group", "ServicePrincipal"], default="User"
    )
    parser.add_argument("--apply", action="store_true", help="Without this flag, print scope only.")
    args = parser.parse_args()
    subscription = str(UUID(args.subscription))
    scope = (
        f"/subscriptions/{subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.App/sandboxGroups/{args.group}"
    )
    print(f"Sandbox group: {scope}\nRegion: {args.region}")
    if args.grant_data_owner_to:
        UUID(args.grant_data_owner_to)
        print(f"Grant Container Apps SandboxGroup Data Owner to {args.grant_data_owner_to}")
    if not args.apply:
        print("No changes made. Add --apply to create resources and requested role assignment.")
        return
    with DefaultAzureCredential() as credential:
        with ResourceManagementClient(credential, subscription) as resources:
            exists = resources.resource_groups.check_existence(args.resource_group)
            if not exists and not args.create_resource_group:
                parser.error(
                    "Resource group does not exist; use --create-resource-group explicitly."
                )
            if not exists:
                resources.resource_groups.create_or_update(
                    args.resource_group,
                    ResourceGroup(location=args.region),
                )
        with SandboxGroupManagementClient(
            credential,
            subscription_id=subscription,
            resource_group=args.resource_group,
            retry_total=0,
            connection_timeout=10,
            read_timeout=60,
        ) as management:
            try:
                group = management.get_group(args.group)
                if group.location.lower() != args.region.lower():
                    parser.error(
                        "Existing sandbox group has a different region; refusing to modify it."
                    )
            except ResourceNotFoundError:
                management.begin_create_group(args.group, location=args.region).result(timeout=300)
        if args.grant_data_owner_to:
            assignment_id = str(uuid5(UUID(DATA_OWNER_ROLE), scope + args.grant_data_owner_to))
            with AuthorizationManagementClient(credential, subscription) as authorization:
                authorization.role_assignments.create(
                    scope=scope,
                    role_assignment_name=assignment_id,
                    parameters=RoleAssignmentCreateParameters(
                        role_definition_id=(
                            f"/subscriptions/{subscription}/providers/"
                            f"Microsoft.Authorization/roleDefinitions/{DATA_OWNER_ROLE}"
                        ),
                        principal_id=args.grant_data_owner_to,
                        principal_type=args.principal_type,
                    ),
                )
    print("Setup complete. Allow role assignments to propagate before calling the data plane.")


if __name__ == "__main__":
    main()
