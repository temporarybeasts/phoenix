/**
 * @generated SignedSource<<420072f19000027a6a6a827c9c8c2ad6>>
 * @lightSyntaxTransform
 */

/* tslint:disable */
/* eslint-disable */
// @ts-nocheck

import { ConcreteRequest } from 'relay-runtime';
export type ChooseProjectGroupPageMutation$variables = {
  projectGroupId: string;
};
export type ChooseProjectGroupPageMutation$data = {
  readonly setActiveProjectGroup: boolean;
};
export type ChooseProjectGroupPageMutation = {
  response: ChooseProjectGroupPageMutation$data;
  variables: ChooseProjectGroupPageMutation$variables;
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
    "name": "ChooseProjectGroupPageMutation",
    "selections": (v1/*:: as any*/),
    "type": "Mutation",
    "abstractKey": null
  },
  "kind": "Request",
  "operation": {
    "argumentDefinitions": (v0/*:: as any*/),
    "kind": "Operation",
    "name": "ChooseProjectGroupPageMutation",
    "selections": (v1/*:: as any*/)
  },
  "params": {
    "cacheID": "249d0e70f6f26292264b78c15d282739",
    "id": null,
    "metadata": {},
    "name": "ChooseProjectGroupPageMutation",
    "operationKind": "mutation",
    "text": "mutation ChooseProjectGroupPageMutation(\n  $projectGroupId: ID!\n) {\n  setActiveProjectGroup(projectGroupId: $projectGroupId)\n}\n"
  }
};
})();

(node as any).hash = "7b96ce9678a89bcb0c51e3c3aad4cfac";

export default node;
