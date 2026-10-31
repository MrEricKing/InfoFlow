# InfoFlow SingleFile MV3 Vendor Artifact

- Source upstream: https://github.com/gildas-lormeau/SingleFile-MV3
- InfoFlow fork: https://github.com/MrEricKing/SingleFile-MV3
- Fork branch: `infoflow-automation-runtime`
- Pinned commit: `d8c0ac26add39adf33643b1cff4299e796237ca0`
- Upstream manifest version: `1.22.98`
- Extension id: `jkeljiefjnokjkioahopjglagkbocpbh`

This directory is an unpacked MV3 extension runtime artifact for InfoFlow's
server-side capture pipeline. It intentionally excludes `node_modules/` and
packaged zip files. The fork branch keeps the source patch history; this
runtime artifact is loaded directly by Playwright persistent Chromium.

InfoFlow-specific behavior is implemented by:

- `manifest.json`: extension name, fixed public key, and
  `externally_connectable.matches = ["<all_urls>"]`.
- `src/core/bg/infoflow-automation.js`: requestId-scoped automation status.
- `src/core/bg/external-messages.js`: `infoflow.capture.*` external API.
- `src/core/bg/download*.js` and `src/core/common/download.js`: download status
  propagation into the request model.

Refresh procedure:

1. Rebase the fork branch on the desired official SingleFile-MV3 commit.
2. Run `npm ci` once if dependencies are missing.
3. Run `./build.sh` in the fork checkout to refresh `lib/` without running
   upstream `npm update`.
4. Smoke-test with Playwright persistent Chromium and the fixed extension id.
5. Copy `manifest.json`, `lib/`, `src/`, `_locales/`, license, README, privacy,
   and InfoFlow notes into this directory.
