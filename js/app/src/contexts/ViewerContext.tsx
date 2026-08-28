import React, { startTransition, useCallback } from "react";
import { graphql, useRefetchableFragment } from "react-relay";

import type {
  ViewerContext_viewer$data,
  ViewerContext_viewer$key,
} from "./__generated__/ViewerContext_viewer.graphql";

export type ViewerContextType = {
  viewer: ViewerContext_viewer$data["viewer"];
  refetchViewer: () => void;
};

export const ViewerContext = React.createContext<ViewerContextType>({
  viewer: null,
  refetchViewer: () => {},
});

export function useViewer() {
  const context = React.useContext(ViewerContext);
  if (context == null) {
    throw new Error("useViewer must be used within a ViewerProvider");
  }
  return context;
}

/**
 * Returns true if the viewer can modify entities in the application
 */
export function useViewerCanModify() {
  const { viewer } = useViewer();
  if (viewer && viewer.role.name === "VIEWER") {
    return false;
  }
  return true;
}

/**
 * Returns true if the viewer is an admin or authentication is disabled.
 * This matches the server-side IsAdminIfAuthEnabled permission.
 */
export function useIsAdminOrAuthDisabled() {
  const isAuthenticatedAdmin = useIsAuthenticatedAdmin();
  return !window.Config.authenticationEnabled || isAuthenticatedAdmin;
}

/**
 * Returns true only for an authenticated admin.
 * This matches the server-side IsAdmin permission.
 */
export function useIsAuthenticatedAdmin() {
  const { viewer } = useViewer();
  return window.Config.authenticationEnabled && viewer?.role?.name === "ADMIN";
}

/**
 * Returns true if the viewer can manage retention policies
 */
export function useViewerCanManageRetentionPolicy() {
  return useIsAdminOrAuthDisabled();
}

/**
 * Returns true if the viewer can manage sandboxes
 */
export function useViewerCanManageSandboxes() {
  return useIsAdminOrAuthDisabled();
}

/**
 * Returns true if the viewer can manage secrets
 */
export function useViewerCanManageSecrets() {
  return useIsAdminOrAuthDisabled();
}

/**
 * Returns true if the viewer should be shown platform version update notices
 */
export function useViewerCanSeeVersionUpdates() {
  return useIsAdminOrAuthDisabled();
}

/**
 * Returns true if the viewer can bulk-delete a project's annotations
 */
export function useViewerCanDeleteProjectAnnotations() {
  return useIsAdminOrAuthDisabled();
}

/**
 * The project group the viewer is currently "viewing" -- null if
 * unresolved (no groups, or 2+ groups with no selection made yet). A
 * newly created project lands in this group.
 */
export function useActiveProjectGroup() {
  const { viewer } = useViewer();
  return viewer?.activeProjectGroup ?? null;
}

/**
 * Every project group the viewer currently holds a role in, via their held
 * external roles -- not just the one they're viewing. Empty for a
 * zero-group viewer.
 */
export function useProjectGroups() {
  const { viewer } = useViewer();
  return viewer?.projectGroups ?? [];
}

/**
 * Returns true if the viewer can create a project right now. Mirrors
 * `create_project` server-side: project-group RBAC only applies at all
 * when some configured OAuth2 client actually captures an IdP groups claim
 * (`projectGroupRbacEnabled`) -- otherwise (auth disabled, or auth enabled
 * but no IdP groups configured anywhere, e.g. plain basic-auth) there's no
 * external-role mapping for anyone, IdP or local, and creation always
 * lands in the default project group. When RBAC *is* in use, creation
 * requires a resolved active group where the viewer holds MEMBER/ADMIN --
 * a global ADMIN account role does not bypass this (per-group role and
 * account role are different things).
 */
export function useCanCreateProject() {
  const activeProjectGroup = useActiveProjectGroup();
  if (!window.Config.authenticationEnabled || !window.Config.projectGroupRbacEnabled) {
    return true;
  }
  return (
    activeProjectGroup != null &&
    (activeProjectGroup.role === "MEMBER" || activeProjectGroup.role === "ADMIN")
  );
}

export function ViewerProvider({
  query,
  children,
}: React.PropsWithChildren<{
  query: ViewerContext_viewer$key;
}>) {
  const [data, _refetch] = useRefetchableFragment(
    graphql`
      fragment ViewerContext_viewer on Query
      @refetchable(queryName: "ViewerContextRefetchQuery") {
        viewer {
          id
          username
          email
          profilePictureUrl
          isManagementUser
          role {
            name
          }
          authMethod
          activeProjectGroup {
            id
            name
            role
          }
          projectGroups {
            id
            name
            role
          }
          ...ViewerAPIKeysListFragment
          ...AuthorizedApplicationsCardFragment
        }
      }
    `,
    query
  );
  const refetchViewer = useCallback(() => {
    startTransition(() => {
      _refetch(
        {},
        {
          fetchPolicy: "network-only",
        }
      );
    });
  }, [_refetch]);
  return (
    <ViewerContext.Provider value={{ viewer: data.viewer, refetchViewer }}>
      {children}
    </ViewerContext.Provider>
  );
}
