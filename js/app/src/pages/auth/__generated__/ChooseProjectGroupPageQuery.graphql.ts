/**
 * @generated SignedSource<<f4ae6488eda265112bf24c38c1569efa>>
 * @lightSyntaxTransform
 */

/* tslint:disable */
/* eslint-disable */
// @ts-nocheck

import { ConcreteRequest } from 'relay-runtime';
export type ChooseProjectGroupPageQuery$variables = Record<PropertyKey, never>;
export type ChooseProjectGroupPageQuery$data = {
  readonly viewer: {
    readonly projectGroups: ReadonlyArray<{
      readonly id: string;
      readonly name: string;
      readonly role: string | null;
    }>;
  } | null;
};
export type ChooseProjectGroupPageQuery = {
  response: ChooseProjectGroupPageQuery$data;
  variables: ChooseProjectGroupPageQuery$variables;
};

const node: ConcreteRequest = (function(){
var v0 = {
  "alias": null,
  "args": null,
  "kind": "ScalarField",
  "name": "id",
  "storageKey": null
},
v1 = {
  "alias": null,
  "args": null,
  "concreteType": "ProjectGroup",
  "kind": "LinkedField",
  "name": "projectGroups",
  "plural": true,
  "selections": [
    (v0/*:: as any*/),
    {
      "alias": null,
      "args": null,
      "kind": "ScalarField",
      "name": "name",
      "storageKey": null
    },
    {
      "alias": null,
      "args": null,
      "kind": "ScalarField",
      "name": "role",
      "storageKey": null
    }
  ],
  "storageKey": null
};
return {
  "fragment": {
    "argumentDefinitions": [],
    "kind": "Fragment",
    "metadata": null,
    "name": "ChooseProjectGroupPageQuery",
    "selections": [
      {
        "alias": null,
        "args": null,
        "concreteType": "User",
        "kind": "LinkedField",
        "name": "viewer",
        "plural": false,
        "selections": [
          (v1/*:: as any*/)
        ],
        "storageKey": null
      }
    ],
    "type": "Query",
    "abstractKey": null
  },
  "kind": "Request",
  "operation": {
    "argumentDefinitions": [],
    "kind": "Operation",
    "name": "ChooseProjectGroupPageQuery",
    "selections": [
      {
        "alias": null,
        "args": null,
        "concreteType": "User",
        "kind": "LinkedField",
        "name": "viewer",
        "plural": false,
        "selections": [
          (v1/*:: as any*/),
          (v0/*:: as any*/)
        ],
        "storageKey": null
      }
    ]
  },
  "params": {
    "cacheID": "9436ce44a9b444f526a6ec066f6e2137",
    "id": null,
    "metadata": {},
    "name": "ChooseProjectGroupPageQuery",
    "operationKind": "query",
    "text": "query ChooseProjectGroupPageQuery {\n  viewer {\n    projectGroups {\n      id\n      name\n      role\n    }\n    id\n  }\n}\n"
  }
};
})();

(node as any).hash = "eca593f9aaf02a382f38f129c175b249";

export default node;
