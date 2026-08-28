import { useState } from "react";
import { graphql, useMutation } from "react-relay";

import {
  Flex,
  Icon,
  Icons,
  Menu,
  MenuButton,
  MenuButtonValue,
  MenuContainer,
  MenuItem,
  MenuTrigger,
  SelectChevronUpDownIcon,
  Text,
} from "@phoenix/components";
import {
  useActiveProjectGroup,
  useNotifyError,
  useProjectGroups,
} from "@phoenix/contexts";
import { getErrorMessagesFromRelayMutationError } from "@phoenix/utils/errorUtils";

import type { ProjectGroupSwitcherSetActiveMutation } from "./__generated__/ProjectGroupSwitcherSetActiveMutation.graphql";

/**
 * Lets a user who belongs to more than one project group switch which one
 * they're currently "viewing" -- a project they create lands in this
 * group, and the project list/dashboards/etc. are scoped to it (see
 * phoenix.server.access.resolution). Renders nothing for a user in 0 or 1
 * groups, since there's nothing to switch between.
 *
 * Switching reloads the page: the active group changes which projects are
 * readable app-wide (RLS-enforced on the backend), and there's no single
 * owner to surgically revalidate every affected query against -- a full
 * reload is the simplest way to guarantee nothing renders stale,
 * project-scoped data from the group the user just left.
 */
export function ProjectGroupSwitcher({
  isExpanded,
}: {
  isExpanded: boolean;
}) {
  const activeProjectGroup = useActiveProjectGroup();
  const projectGroups = useProjectGroups();
  const notifyError = useNotifyError();
  const [isSwitching, setIsSwitching] = useState(false);
  const [commitSetActive] =
    useMutation<ProjectGroupSwitcherSetActiveMutation>(graphql`
      mutation ProjectGroupSwitcherSetActiveMutation(
        $projectGroupId: ID!
      ) {
        setActiveProjectGroup(projectGroupId: $projectGroupId)
      }
    `);

  if (projectGroups.length < 2) {
    return null;
  }

  const onSelectionChange = (projectGroupId: string) => {
    if (projectGroupId === activeProjectGroup?.id) {
      return;
    }
    setIsSwitching(true);
    commitSetActive({
      variables: { projectGroupId },
      onCompleted: () => {
        window.location.reload();
      },
      onError: (error) => {
        setIsSwitching(false);
        notifyError({
          title: "Failed to switch project group",
          message:
            getErrorMessagesFromRelayMutationError(error)?.[0] ??
            "An unexpected error occurred",
        });
      },
    });
  };

  return (
    <MenuTrigger>
      <MenuButton
        aria-label={
          activeProjectGroup
            ? `Project group: ${activeProjectGroup.name}`
            : "Project group"
        }
        leadingVisual={<Icon svg={<Icons.Folder />} />}
        trailingVisual={<SelectChevronUpDownIcon />}
        isDisabled={isSwitching}
      >
        {isExpanded ? (
          activeProjectGroup ? (
            <MenuButtonValue>{activeProjectGroup.name}</MenuButtonValue>
          ) : (
            <MenuButtonValue isPlaceholder>Choose a group</MenuButtonValue>
          )
        ) : null}
      </MenuButton>
      <MenuContainer placement="right bottom">
        <Menu
          aria-label="Project group"
          selectionMode="single"
          disallowEmptySelection
          selectedKeys={activeProjectGroup ? [activeProjectGroup.id] : []}
          onSelectionChange={(keys) => {
            if (keys === "all") {
              return;
            }
            const [next] = keys;
            if (typeof next === "string") {
              onSelectionChange(next);
            }
          }}
        >
          {projectGroups.map((group) => (
            <MenuItem key={group.id} id={group.id} textValue={group.name}>
              <Flex direction="column" gap="size-25">
                <Text weight="heavy">{group.name}</Text>
                {group.role ? (
                  <Text size="XS" color="text-700">
                    {group.role}
                  </Text>
                ) : null}
              </Flex>
            </MenuItem>
          ))}
        </Menu>
      </MenuContainer>
    </MenuTrigger>
  );
}
