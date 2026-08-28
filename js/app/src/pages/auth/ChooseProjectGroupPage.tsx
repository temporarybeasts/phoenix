import { useState } from "react";
import { graphql, useLazyLoadQuery, useMutation } from "react-relay";

import { Alert, Button, Flex, Text, View } from "@phoenix/components";
import { getErrorMessagesFromRelayMutationError } from "@phoenix/utils/errorUtils";
import {
  assignAppRelativeLocation,
  getReturnUrl,
  isServerOwnedPath,
} from "@phoenix/utils/routingUtils";

import type { ChooseProjectGroupPageMutation } from "./__generated__/ChooseProjectGroupPageMutation.graphql";
import type { ChooseProjectGroupPageQuery } from "./__generated__/ChooseProjectGroupPageQuery.graphql";
import { AuthLayout } from "./AuthLayout";
import { PhoenixLogo } from "./PhoenixLogo";

/**
 * Shown after a login (local, LDAP, or OAuth2) for a user who belongs to
 * more than one project group -- they must pick which one they're
 * "viewing" before entering the app, since a project they create lands in
 * that group and the app's project list/dashboards/etc. are scoped to it.
 * Reachable via a full document load (OAuth2's server-side redirect) or an
 * SPA navigation (local/LDAP login's `requiresGroupSelection` response
 * flag), so it fetches its own viewer data rather than relying on
 * AuthenticatedRoot's loader -- this page renders before that shell does.
 */
export function ChooseProjectGroupPage() {
  return (
    <AuthLayout>
      <Flex direction="column" gap="size-200" alignItems="center">
        <View paddingBottom="size-200">
          <PhoenixLogo />
        </View>
      </Flex>
      <ChooseProjectGroup />
    </AuthLayout>
  );
}

function ChooseProjectGroup() {
  const [error, setError] = useState<string | null>(null);
  const [selectingId, setSelectingId] = useState<string | null>(null);
  const data = useLazyLoadQuery<ChooseProjectGroupPageQuery>(
    graphql`
      query ChooseProjectGroupPageQuery {
        viewer {
          projectGroups {
            id
            name
            role
          }
        }
      }
    `,
    {},
    { fetchPolicy: "network-only" }
  );
  const [commitSetActive] =
    useMutation<ChooseProjectGroupPageMutation>(graphql`
      mutation ChooseProjectGroupPageMutation($projectGroupId: ID!) {
        setActiveProjectGroup(projectGroupId: $projectGroupId)
      }
    `);

  const projectGroups = data.viewer?.projectGroups ?? [];

  const onSelect = (projectGroupId: string) => {
    setError(null);
    setSelectingId(projectGroupId);
    commitSetActive({
      variables: { projectGroupId },
      onCompleted: () => {
        const returnUrl = getReturnUrl();
        if (isServerOwnedPath(returnUrl)) {
          assignAppRelativeLocation(returnUrl);
          return;
        }
        // A full document load, not a router navigation: the app shell's
        // loader needs to re-run now that the active-group cookie is set,
        // and this page sits outside the authenticated route tree that
        // loader belongs to.
        assignAppRelativeLocation(returnUrl || "/");
      },
      onError: (mutationError) => {
        setSelectingId(null);
        setError(
          getErrorMessagesFromRelayMutationError(mutationError)?.[0] ??
            "Failed to select project group"
        );
      },
    });
  };

  return (
    <Flex direction="column" gap="size-200">
      <Text>
        You belong to more than one project group. Choose which one you want
        to view -- you can switch later from the app.
      </Text>
      {error ? <Alert variant="danger">{error}</Alert> : null}
      <Flex direction="column" gap="size-100">
        {projectGroups.map((group) => (
          <Button
            key={group.id}
            size="L"
            isDisabled={selectingId != null}
            onPress={() => onSelect(group.id)}
          >
            <Flex direction="column" alignItems="start" width="100%">
              <Text weight="heavy">{group.name}</Text>
              {group.role ? (
                <Text size="XS" color="text-700">
                  {group.role}
                </Text>
              ) : null}
            </Flex>
          </Button>
        ))}
      </Flex>
    </Flex>
  );
}
