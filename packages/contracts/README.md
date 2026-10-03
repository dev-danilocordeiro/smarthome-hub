# @smarthome/contracts

`openapi.json` exported from `apps/api` and the TypeScript types generated from it
(`src/schema.d.ts`). Nothing here is written by hand:

```bash
make gen-client      # export the OpenAPI document, then regenerate the types
```

CI fails if either file is stale (`scripts/export_openapi.py --check` in the backend
workflow, a regenerate-and-diff in the web workflow). The web app uses the types with
`openapi-fetch`, so a renamed field or route breaks `tsc`, not production.

The live WebSocket (`/homes/{id}/live`) is not part of OpenAPI; its messages are typed
in `apps/web/src/live`.
