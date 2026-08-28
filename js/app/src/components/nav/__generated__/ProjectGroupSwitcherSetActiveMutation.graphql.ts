/**
 * @generated SignedSource<<1f0499b2c7c9cedc59fff3454a1c3095>>
 * @lightSyntaxTransform
 */

/* tslint:disable */
/* eslint-disable */
// @ts-nocheck

import { ConcreteRequest } from 'relay-runtime';
export type ProjectGroupSwitcherSetActiveMutation$variables = {
  projectGroupId: string;
};
export type ProjectGroupSwitcherSetActiveMutation$data = {
  readonly setActiveProjectGroup: boolean;
};
export type ProjectGroupSwitcherSetActiveMutation = {
  response: ProjectGroupSwitcherSetActiveMutation$data;
  variables: ProjectGroupSwitcherSetActiveMutation$variables;
};

const node: ConcreteRequest = (function(){
var v0 = [
  {
    "defaultValue": null,
    "kind": "LocalArgument",
    "name": "projectGroupId"
  }
],
v1 = [
  {
    "alias": null,
    "args": [
      {
        "kind": "Variable",
        "name": "projectGroupId",
        "variableName": "projectGroupId"
      }
    ],
    "kind": "ScalarField",
    "name": "setActiveProjectGroup",
    "storageKey": null
  }
];
return {
  "fragment": {
    "argumentDefinitions": (v0/*:: as any*/),
    "kind": "Fragment",
    "metadata": null,
    "name": "ProjectGroupSwitcherSetActiveMutation",
    "selections": (v1/*:: as any*/),
    "type": "Mutation",
    "abstractKey": null
  },
  "kind": "Request",
  "operation": {
    "argumentDefinitions": (v0/*:: as any*/),
    "kind": "Operation",
    "name": "ProjectGroupSwitcherSetActiveMutation",
    "selections": (v1/*:: as any*/)
  },
  "params": {
    "cacheID": "db3d2eca719f5beafd45acd548753031",
    "id": null,
    "metadata": {},
    "name": "ProjectGroupSwitcherSetActiveMutation",
    "operationKind": "mutation",
    "text": "mutation ProjectGroupSwitcherSetActiveMutation(\n  $projectGroupId: ID!\n) {\n  setActiveProjectGroup(projectGroupId: $projectGroupId)\n}\n"
  }
};
})();

(node as any).hash = "61011fafce26b0bb8d6debed290d89b3";

export default node;
