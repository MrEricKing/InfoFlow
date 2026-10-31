# InfoFlow Automation Runtime

This fork keeps the upstream SingleFile MV3 extension as the baseline and adds a
small automation bridge for InfoFlow-Aggregator capture jobs.

## Upstream

- Upstream repository: <https://github.com/gildas-lormeau/SingleFile-MV3>
- Baseline branch: `main`
- Automation branch: `infoflow-automation-runtime`
- License: AGPL-3.0-or-later, inherited from SingleFile

## Extension ID

The manifest contains an InfoFlow-generated public key so the unpacked extension
has a stable runtime id:

```text
jkeljiefjnokjkioahopjglagkbocpbh
```

The private key used to generate the manifest key is not stored in this
repository. Regenerate a new key only if the extension id is intentionally
rotated, and update InfoFlow-Aggregator runtime configuration at the same time.

## Automation API

`manifest.json` exposes `externally_connectable.matches = ["<all_urls>"]` so the
active capture page can call `chrome.runtime.sendMessage(extensionId, message)`.
The background external-message bridge accepts only the InfoFlow methods below;
upstream generic external actions and autosave messages are intentionally not
part of this runtime surface.

InfoFlow-specific methods:

- `infoflow.capture.start`
- `infoflow.capture.status`
- `infoflow.capture.list`
- `infoflow.capture.cancel`

Start message shape:

```json
{
  "method": "infoflow.capture.start",
  "requestId": "uuid-or-operation-id",
  "options": {}
}
```

The bridge starts SingleFile against the current active tab, injects
`infoflowRequestId` into the existing SingleFile options, and records a
request-scoped status object. The download path still uses the browser
`downloads` API, but the fork records download request/start/complete/error
events and exposes them through `infoflow.capture.status`.

The backend should treat extension status as the primary automation contract and
perform its own filesystem and saved-HTML validation before accepting a capture
as complete.

## Intentional Non-Changes

- The fork does not use the `Cosmologist/SingleFile-MV3-CDP-Patched` fixed id.
- The fork does not replace the downloader with a simple `<a download>` bridge.
- The fork does not change `blockVideos` defaults for personal convenience.
