# NoUI Workflow Recorder (Chrome extension)

Captures HAR traffic, clicks, and URL events from authenticated browser
sessions, and posts them to the NoUI backend for compilation into MCP servers.

## Install (load unpacked)

v1 ships load-unpacked only — there is no Chrome Web Store listing yet.

```
Chrome → chrome://extensions → Developer mode → Load unpacked → select this folder
```

## Configuration

The extension talks to the NoUI backend at `http://localhost:8002` by default.

To point it at a different backend, open the extension popup → Settings, or
from the DevTools console on any extension page:

```js
chrome.storage.sync.set({ nouiBackendUrl: "http://my-noui-host:8002" });
```

The service worker picks up changes live via `chrome.storage.onChanged`.

## Permissions

| Permission | Why |
|---|---|
| `<all_urls>` host permission | HAR capture works across arbitrary user-chosen origins. Scoping this down would require the user to manually approve every recorded site. |
| `debugger` | Needed to attach the Chrome DevTools Protocol for HAR capture. |
| `webRequest` | Captures request metadata for HAR assembly. |
| `webNavigation` | Tracks URL transitions as navigation events. |
| `scripting` | Injects the narration / login recorder content scripts. |
| `storage` | Persists capture state across service-worker restarts and stores the configurable backend URL. |
| `activeTab`, `tabs`, `sidePanel` | Popup and side-panel UI. |

The extension does not transmit captured data anywhere except the configured
NoUI backend.
