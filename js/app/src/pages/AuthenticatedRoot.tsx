import { useEffect } from "react";
import { Navigate, Outlet, useLoaderData } from "react-router";
import invariant from "tiny-invariant";

import { AgentContextSync } from "@phoenix/agent/context/AgentContextSync";
import { RootUIOperationsRegistration } from "@phoenix/agent/uiOperations/RootUIOperationsRegistration";
import { isFullStoryEnabled, setIdentity } from "@phoenix/analytics/fullstory";
import { AgentChatRuntimeProvider } from "@phoenix/contexts/AgentChatRuntimeContext";
import { AgentProvider } from "@phoenix/contexts/AgentContext";
import { ViewerProvider } from "@phoenix/contexts/ViewerContext";
import { useOwnedPreloadedQuery } from "@phoenix/hooks";
import type { authenticatedRootLoaderQuery } from "@phoenix/pages/__generated__/authenticatedRootLoaderQuery.graphql";
import {
  authenticatedRootLoaderQueryNode,
  type AuthenticatedRootLoaderData,
} from "@phoenix/pages/authenticatedRootLoader";
import { createRedirectUrlWithReturn } from "@phoenix/utils/routingUtils";

import { AppAlerts } from "./AppAlerts";

/**
 * The root of the authenticated application. Note that authentication might be entirely disabled
 */
export function AuthenticatedRoot() {
  const loaderData = useLoaderData<AuthenticatedRootLoaderData>();
  invariant(loaderData, "loaderData is required");
  const data = useOwnedPreloadedQuery<authenticatedRootLoaderQuery>({
    query: authenticatedRootLoaderQueryNode,
    queryRef: loaderData.queryRef,
  });

  // Set analytics if enabled
  useEffect(() => {
    // Double check that there is a viewer and that FullStory is enabled
    if (isFullStoryEnabled() && data.viewer) {
      setIdentity({
        uid: data.viewer.id,
        displayName: data.viewer.username,
        email: data.viewer.email,
      });
    }
  }, [data.viewer]);

  if (data.viewer?.passwordNeedsReset) {
    return (
      <Navigate
        to={createRedirectUrlWithReturn({ path: "/reset-password" })}
        replace
      />
    );
  }

  // A user in 2+ project groups must explicitly pick which one they're
  // viewing (see phoenix.server.access.resolution) -- normally handled by
  // the login flow itself (a redirect/response flag from /auth/login,
  // /auth/ldap/login, or the OAuth2 callback), but this is the safety net
  // for reaching the authenticated shell some other way (e.g. a stale
  // active-group cookie cleared mid-session, or a direct SPA navigation
  // that never passed through a login response).
  if (
    data.viewer &&
    data.viewer.activeProjectGroup == null &&
    data.viewer.projectGroups.length > 1
  ) {
    return (
      <Navigate
        to={createRedirectUrlWithReturn({ path: "/login/choose-group" })}
        replace
      />
    );
  }

  return (
    <ViewerProvider query={data}>
      <AgentProvider agentsConfig={data.agentsConfig}>
        <AgentChatRuntimeProvider>
          <AgentContextSync />
          <RootUIOperationsRegistration />
          <AppAlerts />
          <Outlet />
        </AgentChatRuntimeProvider>
      </AgentProvider>
    </ViewerProvider>
  );
}
