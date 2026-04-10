/**
 * Background service worker for the Elicitation Agent extension.
 *
 * Generic API proxy + screenshot capture + HAR capture via chrome.webRequest.
 */

const BACKEND_URL = "http://localhost:8002";

// Track which tab has the voice recognizer injected
let voiceActiveTabId = null;

// URL monitoring state
let urlMonitoringState = null; // { projectId, processId, captureSessionId, lastUrl }

// Capture state — used to inject project/process/session IDs into click events
let captureState = null; // { projectId, processId, captureSessionId }

// HAR capture state
let harState = null; // { tabId, requestMap: {}, captureSessionId }

// Narration capture state
let narrationCaptureState = null; // { tabId, active: true }

// Login recording mode state
let loginRecordingState = null; // { captureSessionId, projectId, processId }

// ── Session storage persistence (survives MV3 worker restarts) ──────────

let _harPersistTimer = null;
const HAR_PERSIST_INTERVAL = 2000; // ms

// Restore harState + captureState + urlMonitoringState + narrationCaptureState on worker startup
chrome.storage.session.get(["harMeta", "harRequests", "captureState", "urlMonitoringState", "narrationCaptureState", "loginRecordingState"], (result) => {
  if (!result) return;
  if (result.harMeta?.active) {
    harState = {
      tabId: result.harMeta.tabId,
      captureSessionId: result.harMeta.captureSessionId,
      requestMap: result.harRequests || {},
    };
    console.log("[HAR] Restored from session storage — tab:", harState.tabId,
      "requests:", Object.keys(harState.requestMap).length);
  }
  if (result.captureState) {
    captureState = result.captureState;
  }
  if (result.urlMonitoringState) {
    urlMonitoringState = result.urlMonitoringState;
  }
  if (result.narrationCaptureState) {
    narrationCaptureState = result.narrationCaptureState;
  }
  if (result.loginRecordingState) {
    loginRecordingState = result.loginRecordingState;
  }
});

function _persistHarRequests() {
  if (_harPersistTimer) return;
  _harPersistTimer = setTimeout(() => {
    _harPersistTimer = null;
    if (harState) {
      chrome.storage.session.set({ harRequests: harState.requestMap });
    }
  }, HAR_PERSIST_INTERVAL);
}

function _flushHarRequests() {
  if (_harPersistTimer) {
    clearTimeout(_harPersistTimer);
    _harPersistTimer = null;
  }
  if (harState) {
    chrome.storage.session.set({ harRequests: harState.requestMap });
  }
}

function _persistHarMeta() {
  if (harState) {
    chrome.storage.session.set({
      harMeta: { tabId: harState.tabId, captureSessionId: harState.captureSessionId, active: true },
    });
  } else {
    chrome.storage.session.remove(["harMeta", "harRequests"]);
  }
}

function _persistCaptureState() {
  if (captureState) {
    chrome.storage.session.set({ captureState });
  } else {
    chrome.storage.session.remove(["captureState"]);
  }
}

// ── Message router ──────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handler = handlers[message.type];
  if (handler) {
    handler(message)
      .then((data) => sendResponse({ success: true, data }))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true; // async response
  }

  // Intercept voice events for narration capture
  if (message.type === "VOICE_FINAL" && narrationCaptureState?.active && captureState) {
    // Send narration to backend (fire-and-forget)
    backendFetch("POST", "/narrations", {
      project_id: captureState.projectId,
      process_id: captureState.processId,
      capture_session_id: captureState.captureSessionId,
      content: message.transcript,
      url: message.url || "",
    }).catch((e) => console.warn("[Narration] Failed to save:", e.message));
    // Still forward to popup for UI feedback
    return false;
  }

  if (message.type === "VOICE_ENDED" && narrationCaptureState?.active) {
    // Auto-restart voice recognition after a short delay
    setTimeout(() => {
      if (!narrationCaptureState?.active) return;
      const tabId = narrationCaptureState.tabId;
      chrome.scripting.executeScript({ target: { tabId }, files: ["content/voice-recognizer.js"] })
        .then(() => console.log("[Narration] Voice recognizer re-injected on tab", tabId))
        .catch((e) => console.warn("[Narration] Failed to re-inject voice recognizer:", e.message));
    }, 500);
    return false;
  }

  if (["VOICE_INTERIM", "VOICE_FINAL", "VOICE_ENDED", "VOICE_ERROR", "MIC_PERMISSION_GRANTED", "REGION_SELECTED", "REGION_CANCELLED"].includes(message.type)) {
    return false;
  }

  if (message.type === "PING_BACKEND") {
    backendFetch("POST", "/sessions/ping", { message: message.payload || "hello" })
      .then((data) => sendResponse({ success: true, data }))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }
});

// ── Handlers ────────────────────────────────────────────────────────────────

const handlers = {
  BACKEND_API: async ({ method, path, body }) => {
    return backendFetch(method, path, body);
  },

  CAPTURE_SCREENSHOT: async ({ projectId, processId }) => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const tabUrl = tab?.url || "";

    if (tab?.url) {
      try {
        const origin = new URL(tab.url).origin + "/*";
        const hasPermission = await chrome.permissions.contains({ origins: [origin] });
        if (!hasPermission) {
          const granted = await chrome.permissions.request({ origins: [origin] });
          if (!granted) {
            throw new Error("Screenshot permission denied.");
          }
        }
      } catch (permErr) {
        console.warn("[Screenshot] Permission check failed:", permErr.message);
      }
    }

    const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: "png" });
    const response = await fetch(dataUrl);
    const blob = await response.blob();

    const formData = new FormData();
    formData.append("file", blob, "screenshot.png");
    if (projectId) formData.append("project_id", projectId);
    if (processId) formData.append("process_id", processId);
    formData.append("url", tabUrl);

    const uploadResp = await fetch(`${BACKEND_URL}/screenshots`, {
      method: "POST",
      body: formData,
    });

    if (!uploadResp.ok) {
      throw new Error(`Screenshot upload failed: ${uploadResp.status}`);
    }

    return uploadResp.json();
  },

  // ── HAR capture via chrome.webRequest (NOT chrome.debugger) ──────────

  START_HAR_CAPTURE: async ({ captureSessionId, tabId }) => {
    if (!tabId) throw new Error("No tabId provided for HAR capture");

    // Clean up previous state
    if (harState) {
      console.log("[HAR] Replacing previous capture on tab", harState.tabId);
    }

    const tab = await chrome.tabs.get(tabId).catch(() => null);
    console.log("[HAR] Starting webRequest capture on tab", tabId, "URL:", tab?.url || "unknown");

    harState = { tabId, requestMap: {}, captureSessionId };
    _persistHarMeta();

    // Emit timeline event
    if (captureState) {
      backendFetch("POST", "/timeline", {
        project_id: captureState.projectId,
        process_id: captureState.processId,
        capture_session_id: captureSessionId,
        event_type: "har_start",
        summary: "Network (HAR) capture started",
      }).catch(() => {});
    }

    return { status: "started", tabId };
  },

  STOP_HAR_CAPTURE: async ({ captureSessionId }) => {
    // If harState is null (worker restarted), restore from session storage
    if (!harState) {
      const stored = await chrome.storage.session.get(["harMeta", "harRequests"]);
      if (stored.harMeta?.active) {
        harState = {
          tabId: stored.harMeta.tabId,
          captureSessionId: stored.harMeta.captureSessionId,
          requestMap: stored.harRequests || {},
        };
        console.log("[HAR] Restored for stop — requests:", Object.keys(harState.requestMap).length);
      } else {
        return { status: "no_capture", entries: 0 };
      }
    }

    _flushHarRequests();

    const { requestMap } = harState;
    const sessionId = harState.captureSessionId || captureSessionId;

    // Assemble HAR 1.2 (spec: http://www.softwareishard.com/blog/har-12-spec/)
    const entries = [];
    for (const [, req] of Object.entries(requestMap)) {
      const url = req.url || "";
      // Filter out extension requests (backend API calls are kept — they're
      // legitimate traffic when the target site is on the same host)
      if (url.startsWith("chrome-extension://")) continue;

      // Parse query string
      let queryString = [];
      try {
        const parsed = new URL(url);
        for (const [name, value] of parsed.searchParams.entries()) {
          queryString.push({ name, value });
        }
      } catch (_) {}

      // Build postData
      let postData;
      if (req.postBody) {
        postData = {
          mimeType: req.postMimeType || "application/octet-stream",
          text: req.postBody,
        };
      }

      const entry = {
        startedDateTime: req.startedDateTime || new Date().toISOString(),
        time: req.time || 0,
        request: {
          method: req.method || "GET",
          url,
          httpVersion: "HTTP/1.1",
          cookies: [],
          headers: req.requestHeaders || [],
          queryString,
          headersSize: -1,
          bodySize: req.postBody ? req.postBody.length : 0,
        },
        response: {
          status: req.statusCode || 0,
          statusText: req.statusText || "",
          httpVersion: req.responseHttpVersion || "HTTP/1.1",
          cookies: [],
          headers: req.responseHeaders || [],
          content: {
            size: req.responseSize || 0,
            mimeType: req.responseMimeType || "",
          },
          redirectURL: req.redirectUrl || "",
          headersSize: -1,
          bodySize: req.responseSize || -1,
        },
        cache: {},
        timings: { send: 0, wait: req.time || 0, receive: 0 },
      };
      if (postData) entry.request.postData = postData;
      entries.push(entry);
    }

    const har = {
      log: {
        version: "1.2",
        creator: { name: "Adopt.ai Workflow Onboarding Agent", version: "0.3.2" },
        pages: [],
        entries,
      },
    };

    harState = null;
    _persistHarMeta(); // clear session storage

    // Upload HAR to backend
    if (sessionId && entries.length > 0) {
      try {
        const harBlob = new Blob([JSON.stringify(har, null, 2)], { type: "application/json" });
        const formData = new FormData();
        formData.append("file", harBlob, `capture-${sessionId}.har`);
        const resp = await fetch(`${BACKEND_URL}/capture-sessions/${sessionId}/har`, {
          method: "POST",
          body: formData,
        });
        if (!resp.ok) console.warn("[HAR] Upload failed:", resp.status);
      } catch (e) {
        console.warn("[HAR] Upload error:", e.message);
      }
    }

    // Emit timeline event
    if (captureState || sessionId) {
      const pid = captureState?.projectId;
      const procId = captureState?.processId;
      if (pid) {
        backendFetch("POST", "/timeline", {
          project_id: pid,
          process_id: procId,
          capture_session_id: sessionId,
          event_type: "har_stop",
          summary: `Network (HAR) capture stopped — ${entries.length} request${entries.length !== 1 ? "s" : ""} recorded`,
          metadata_json: JSON.stringify({ entry_count: entries.length }),
        }).catch(() => {});
      }
    }

    console.log("[HAR] Stopped, collected", entries.length, "entries");
    return { status: "stopped", entries: entries.length };
  },

  VOICE_START: async () => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found — navigate to a web page first");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url?.startsWith("chrome://") || tab?.url?.startsWith("chrome-extension://")) {
      throw new Error("Voice recording requires a regular web page.");
    }
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content/voice-recognizer.js"] });
    voiceActiveTabId = tabId;
    return { status: "started", tabId };
  },

  VOICE_STOP: async () => {
    if (voiceActiveTabId) {
      try {
        await chrome.tabs.sendMessage(voiceActiveTabId, { type: "CONTENT_VOICE_STOP" });
      } catch (e) {
        console.warn("[Voice] Failed to send stop to tab:", e.message);
      }
      voiceActiveTabId = null;
    }
    return { status: "stopped" };
  },

  INJECT_CLICK_TRACKER: async ({ tabId }) => {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) throw new Error("No active tab found");
    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, files: ["content/click-tracker.js"] });
    return { status: "injected", tabId: targetTabId };
  },

  REMOVE_CLICK_TRACKER: async ({ tabId }) => {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) throw new Error("No active tab found");
    await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      func: () => { if (window.__adoptClickTracker) window.__adoptClickTracker(); },
    });
    return { status: "removed", tabId: targetTabId };
  },

  INJECT_LOGIN_RECORDER: async ({ tabId }) => {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) throw new Error("No active tab found");
    await chrome.scripting.executeScript({ target: { tabId: targetTabId }, files: ["content/login-recorder.js"] });
    return { status: "injected", tabId: targetTabId };
  },

  REMOVE_LOGIN_RECORDER: async ({ tabId }) => {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) throw new Error("No active tab found");
    await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      func: () => { if (window.__adoptLoginRecorder) window.__adoptLoginRecorder(); },
    });
    return { status: "removed", tabId: targetTabId };
  },

  SET_LOGIN_RECORDING_STATE: async ({ captureSessionId, projectId, processId }) => {
    loginRecordingState = { captureSessionId, projectId, processId };
    chrome.storage.session.set({ loginRecordingState });
    return { status: "set" };
  },

  CLEAR_LOGIN_RECORDING_STATE: async () => {
    loginRecordingState = null;
    chrome.storage.session.remove(["loginRecordingState"]);
    return { status: "cleared" };
  },

  GET_LOGIN_RECORDING_STATE: async () => {
    return { state: loginRecordingState };
  },

  LOGIN_CLICK_EVENT: async ({ data }) => {
    if (loginRecordingState) {
      data.project_id = data.project_id || loginRecordingState.projectId;
      data.process_id = data.process_id || loginRecordingState.processId;
      data.capture_session_id = data.capture_session_id || loginRecordingState.captureSessionId;
    } else if (captureState) {
      data.project_id = data.project_id || captureState.projectId;
      data.process_id = data.process_id || captureState.processId;
      data.capture_session_id = data.capture_session_id || captureState.captureSessionId;
    }
    return backendFetch("POST", "/clicks", data);
  },

  LOGIN_INPUT_EVENT: async ({ data }) => {
    if (loginRecordingState) {
      data.project_id = data.project_id || loginRecordingState.projectId;
      data.process_id = data.process_id || loginRecordingState.processId;
      data.capture_session_id = data.capture_session_id || loginRecordingState.captureSessionId;
    } else if (captureState) {
      data.project_id = data.project_id || captureState.projectId;
      data.process_id = data.process_id || captureState.processId;
      data.capture_session_id = data.capture_session_id || captureState.captureSessionId;
    }
    return backendFetch("POST", "/clicks", data);
  },

  CLICK_EVENT: async ({ data }) => {
    if (captureState) {
      data.project_id = data.project_id || captureState.projectId;
      data.process_id = data.process_id || captureState.processId;
      data.capture_session_id = data.capture_session_id || captureState.captureSessionId;
    }
    return backendFetch("POST", "/clicks", data);
  },

  INPUT_EVENT: async ({ data }) => {
    if (captureState) {
      data.project_id = data.project_id || captureState.projectId;
      data.process_id = data.process_id || captureState.processId;
      data.capture_session_id = data.capture_session_id || captureState.captureSessionId;
    }
    return backendFetch("POST", "/clicks", data);
  },

  SET_CAPTURE_STATE: async ({ projectId, processId, captureSessionId }) => {
    captureState = { projectId, processId, captureSessionId };
    _persistCaptureState();
    return { status: "set" };
  },

  CLEAR_CAPTURE_STATE: async () => {
    captureState = null;
    _persistCaptureState();
    return { status: "cleared" };
  },

  GET_CURRENT_URL: async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return { url: tab?.url || "" };
  },

  START_URL_MONITORING: async ({ projectId, processId, captureSessionId }) => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    urlMonitoringState = { projectId, processId, captureSessionId, lastUrl: tab?.url || "" };
    chrome.storage.session.set({ urlMonitoringState });
    return { status: "started" };
  },

  STOP_URL_MONITORING: async () => {
    urlMonitoringState = null;
    chrome.storage.session.remove(["urlMonitoringState"]);
    return { status: "stopped" };
  },

  START_NARRATION_CAPTURE: async ({ tabId }) => {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) throw new Error("No active tab found for narration");
    narrationCaptureState = { tabId: targetTabId, active: true };
    chrome.storage.session.set({ narrationCaptureState });
    // Inject voice recognizer to start listening
    try {
      await chrome.scripting.executeScript({ target: { tabId: targetTabId }, files: ["content/voice-recognizer.js"] });
    } catch (e) {
      console.warn("[Narration] Failed to inject voice recognizer:", e.message);
    }
    voiceActiveTabId = targetTabId;
    console.log("[Narration] Capture started on tab", targetTabId);
    return { status: "started", tabId: targetTabId };
  },

  STOP_NARRATION_CAPTURE: async () => {
    if (narrationCaptureState?.tabId) {
      try {
        await chrome.tabs.sendMessage(narrationCaptureState.tabId, { type: "CONTENT_VOICE_STOP" });
      } catch (e) {
        console.warn("[Narration] Failed to send stop:", e.message);
      }
    }
    narrationCaptureState = null;
    chrome.storage.session.remove(["narrationCaptureState"]);
    voiceActiveTabId = null;
    console.log("[Narration] Capture stopped");
    return { status: "stopped" };
  },

  // ── Autopilot composite commands ──────────────────────────────────────

  START_CAPTURE_SESSION: async ({ projectId, processId, captureSessionId, tabId: requestedTabId }) => {
    const tabId = requestedTabId || (await getActiveTabId());
    if (!tabId) throw new Error("No active tab found for capture");

    // 1. Set capture state
    captureState = { projectId, processId, captureSessionId };
    _persistCaptureState();

    // 2. Start HAR capture (must happen before click tracker — executeScript
    //    can throw on restricted pages like chrome:// or about:blank, and we
    //    must not let that prevent HAR recording from starting)
    harState = { tabId, requestMap: {}, captureSessionId };
    _persistHarMeta();

    // 3. Start URL monitoring
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    urlMonitoringState = { projectId, processId, captureSessionId, lastUrl: tab?.url || "" };
    chrome.storage.session.set({ urlMonitoringState });

    // 4. Inject click tracker (best-effort — may fail on restricted pages)
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content/click-tracker.js"] });
    } catch (e) {
      console.warn("[Autopilot] Click tracker injection failed (will retry on navigate):", e.message);
    }

    console.log("[Autopilot] Capture session started:", captureSessionId);
    return { status: "started", tabId, captureSessionId };
  },

  STOP_CAPTURE_SESSION: async ({ captureSessionId }) => {
    // 1. Stop HAR capture (must be first — uploads HAR on stop)
    let harResult = { status: "no_capture", entries: 0 };
    if (harState || captureSessionId) {
      try {
        harResult = await handlers.STOP_HAR_CAPTURE({ captureSessionId });
      } catch (e) {
        console.warn("[Autopilot] HAR stop failed:", e.message);
      }
    }

    // 2. Remove click tracker
    const tabId = await getActiveTabId();
    if (tabId) {
      try {
        await chrome.scripting.executeScript({
          target: { tabId },
          func: () => { if (window.__adoptClickTracker) window.__adoptClickTracker(); },
        });
      } catch (e) {
        console.warn("[Autopilot] Click tracker removal failed:", e.message);
      }
    }

    // 3. Clear capture state
    captureState = null;
    _persistCaptureState();

    // 4. Stop URL monitoring
    urlMonitoringState = null;
    chrome.storage.session.remove(["urlMonitoringState"]);

    console.log("[Autopilot] Capture session stopped:", captureSessionId);
    return { status: "stopped", har: harResult };
  },

  START_LOGIN_RECORDING_SESSION: async ({ captureSessionId, projectId, processId, tabId: requestedTabId }) => {
    const tabId = requestedTabId || (await getActiveTabId());
    if (!tabId) throw new Error("No active tab found for login recording");

    // 1. Set login recording state
    loginRecordingState = { captureSessionId, projectId, processId };
    chrome.storage.session.set({ loginRecordingState });

    // 2. Start HAR capture (must happen before script injection — executeScript
    //    can throw on restricted pages, and HAR recording is critical)
    harState = { tabId, requestMap: {}, captureSessionId };
    _persistHarMeta();

    // 3. Inject login recorder (best-effort — may fail on restricted pages)
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content/login-recorder.js"] });
    } catch (e) {
      console.warn("[Autopilot] Login recorder injection failed (will retry on navigate):", e.message);
    }

    console.log("[Autopilot] Login recording session started:", captureSessionId);
    return { status: "started", tabId, captureSessionId };
  },

  STOP_LOGIN_RECORDING_SESSION: async ({ captureSessionId }) => {
    // 1. Stop HAR capture (must be first)
    let harResult = { status: "no_capture", entries: 0 };
    if (harState || captureSessionId) {
      try {
        harResult = await handlers.STOP_HAR_CAPTURE({ captureSessionId });
      } catch (e) {
        console.warn("[Autopilot] Login HAR stop failed:", e.message);
      }
    }

    // 2. Remove login recorder
    const tabId = await getActiveTabId();
    if (tabId) {
      try {
        await chrome.scripting.executeScript({
          target: { tabId },
          func: () => { if (window.__adoptLoginRecorder) window.__adoptLoginRecorder(); },
        });
      } catch (e) {
        console.warn("[Autopilot] Login recorder removal failed:", e.message);
      }
    }

    // 3. Clear login recording state
    loginRecordingState = null;
    chrome.storage.session.remove(["loginRecordingState"]);

    console.log("[Autopilot] Login recording session stopped:", captureSessionId);
    return { status: "stopped", har: harResult };
  },

  CAPTURE_REGION_SCREENSHOT: async ({ projectId, processId }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content/region-selector.js"] });

    const rect = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        chrome.runtime.onMessage.removeListener(listener);
        reject(new Error("Region selection timed out"));
      }, 30000);
      function listener(msg) {
        if (msg.type === "REGION_SELECTED") {
          clearTimeout(timeout);
          chrome.runtime.onMessage.removeListener(listener);
          resolve(msg.rect);
        } else if (msg.type === "REGION_CANCELLED") {
          clearTimeout(timeout);
          chrome.runtime.onMessage.removeListener(listener);
          reject(new Error("Region selection cancelled"));
        }
      }
      chrome.runtime.onMessage.addListener(listener);
    });

    const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: "png" });
    const response = await fetch(dataUrl);
    const imageBitmap = await createImageBitmap(await response.blob());
    const dpr = rect.devicePixelRatio || 1;
    const sx = Math.round(rect.x * dpr);
    const sy = Math.round(rect.y * dpr);
    const sw = Math.round(rect.width * dpr);
    const sh = Math.round(rect.height * dpr);
    const canvas = new OffscreenCanvas(sw, sh);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(imageBitmap, sx, sy, sw, sh, 0, 0, sw, sh);
    const croppedBlob = await canvas.convertToBlob({ type: "image/png" });

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const tabUrl = tab?.url || "";
    const formData = new FormData();
    formData.append("file", croppedBlob, "region-screenshot.png");
    if (projectId) formData.append("project_id", projectId);
    if (processId) formData.append("process_id", processId);
    formData.append("url", tabUrl);

    const uploadResp = await fetch(`${BACKEND_URL}/screenshots`, { method: "POST", body: formData });
    if (!uploadResp.ok) throw new Error(`Screenshot upload failed: ${uploadResp.status}`);
    return uploadResp.json();
  },
};

// ── Helper: get active tab ID ────────────────────────────────────────────────

async function getActiveTabId() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab?.id || null;
}

// ── Backend fetch helper ────────────────────────────────────────────────────

async function backendFetch(method, path, body) {
  const opts = { method: method || "GET", headers: {} };
  if (body && method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const response = await fetch(`${BACKEND_URL}${path}`, opts);
  if (response.status === 204) return null;
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Backend ${response.status}: ${text}`);
  }
  return response.json();
}

// ── Network timeline event emitter ──────────────────────────────────────

function _emitNetworkTimelineEvent(entry) {
  if (!captureState) return;
  try {
    const urlObj = new URL(entry.url);
    const path = urlObj.pathname + urlObj.search;
    const method = entry.method || "GET";
    const status = entry.statusCode || 0;
    const duration = entry.time || 0;
    const summary = `${method} ${path} → ${status} (${duration}ms)`;

    const metadata = {
      url: entry.url,
      method,
      status_code: status,
      duration_ms: duration,
      response_mime_type: entry.responseMimeType || "",
      response_size: entry.responseSize || 0,
    };

    // Include request body preview for mutation methods
    if (["POST", "PUT", "PATCH"].includes(method) && entry.postBody) {
      metadata.request_body_preview = entry.postBody.substring(0, 200);
    }

    backendFetch("POST", "/timeline", {
      project_id: captureState.projectId,
      process_id: captureState.processId,
      capture_session_id: captureState.captureSessionId,
      event_type: "network_request",
      summary,
      metadata_json: JSON.stringify(metadata),
    }).catch(() => {}); // fire-and-forget
  } catch (_) {
    // Ignore URL parse errors or other failures
  }
}

// ── HAR: chrome.webRequest listeners (MV3-native, reliable) ─────────────
//
// These top-level listeners persist across service worker restarts.
// They fire for ALL requests but only record when harState is active
// and the tabId matches the captured tab.

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (!harState || details.tabId !== harState.tabId) return;
    if (details.url.startsWith("chrome-extension://")) return;
    if (details.url.startsWith("data:")) return;

    // Extract POST body if available
    let postBody = null;
    let postMimeType = null;
    if (details.requestBody) {
      if (details.requestBody.formData) {
        const params = [];
        for (const [key, values] of Object.entries(details.requestBody.formData)) {
          for (const val of values) {
            params.push(`${encodeURIComponent(key)}=${encodeURIComponent(val)}`);
          }
        }
        postBody = params.join("&");
        postMimeType = "application/x-www-form-urlencoded";
      } else if (details.requestBody.raw && details.requestBody.raw.length > 0) {
        try {
          const decoder = new TextDecoder();
          const parts = details.requestBody.raw.map(r => r.bytes ? decoder.decode(r.bytes) : "");
          postBody = parts.join("");
          postMimeType = "application/octet-stream";
        } catch (_) {}
      }
    }

    harState.requestMap[details.requestId] = {
      url: details.url,
      method: details.method,
      startedDateTime: new Date(details.timeStamp).toISOString(),
      startTimestamp: details.timeStamp,
      type: details.type,
      postBody,
      postMimeType,
      requestHeaders: [],
      responseHeaders: [],
      statusCode: 0,
      statusText: "",
      responseHttpVersion: "HTTP/1.1",
      responseMimeType: "",
      responseSize: 0,
      redirectUrl: "",
      time: 0,
    };
    _persistHarRequests();
  },
  { urls: ["<all_urls>"] },
  ["requestBody"]
);

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (!harState || details.tabId !== harState.tabId) return;
    const entry = harState.requestMap[details.requestId];
    if (entry) {
      entry.requestHeaders = (details.requestHeaders || []).map(h => ({
        name: h.name,
        value: h.value || "",
      }));
      // Extract Content-Type for POST data
      if (entry.postBody && !entry.postMimeType) {
        const ct = details.requestHeaders?.find(h => h.name.toLowerCase() === "content-type");
        if (ct) entry.postMimeType = ct.value;
      }
    }
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"]
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!harState || details.tabId !== harState.tabId) return;
    const entry = harState.requestMap[details.requestId];
    if (entry) {
      entry.statusCode = details.statusCode;
      // Parse status line: "HTTP/1.1 200 OK"
      if (details.statusLine) {
        const parts = details.statusLine.split(" ");
        entry.responseHttpVersion = parts[0] || "HTTP/1.1";
        entry.statusText = parts.slice(2).join(" ");
      }
      entry.responseHeaders = (details.responseHeaders || []).map(h => ({
        name: h.name,
        value: h.value || "",
      }));
      // Extract content-type and content-length
      for (const h of details.responseHeaders || []) {
        const lower = h.name.toLowerCase();
        if (lower === "content-type") entry.responseMimeType = (h.value || "").split(";")[0].trim();
        if (lower === "content-length") entry.responseSize = parseInt(h.value, 10) || 0;
      }
      _persistHarRequests();
    }
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders", "extraHeaders"]
);

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (!harState || details.tabId !== harState.tabId) return;
    const entry = harState.requestMap[details.requestId];
    if (entry) {
      entry.time = Math.round(details.timeStamp - entry.startTimestamp);
      _persistHarRequests();

      // Emit real-time network timeline event for XHR/fetch requests
      if (captureState && entry.type === "xmlhttprequest") {
        _emitNetworkTimelineEvent(entry);
      }
    }
  },
  { urls: ["<all_urls>"] }
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    if (!harState || details.tabId !== harState.tabId) return;
    // Remove failed requests from the map
    delete harState.requestMap[details.requestId];
  },
  { urls: ["<all_urls>"] }
);

// ── URL monitoring via webNavigation ───────────────────────────────────────

chrome.webNavigation.onCompleted.addListener((details) => {
  if (details.frameId !== 0) return;

  if (!urlMonitoringState) return;
  if (details.url === urlMonitoringState.lastUrl) return;

  const fromUrl = urlMonitoringState.lastUrl;
  urlMonitoringState.lastUrl = details.url;

  backendFetch("POST", "/url-events", {
    project_id: urlMonitoringState.projectId,
    process_id: urlMonitoringState.processId,
    capture_session_id: urlMonitoringState.captureSessionId,
    from_url: fromUrl,
    to_url: details.url,
  }).catch((err) => {
    console.warn("[URL Monitor] Failed to record URL change:", err.message);
  });

  // Re-inject click tracker on navigation when capture is active
  if (captureState && harState && details.tabId === harState.tabId) {
    setTimeout(() => {
      if (!captureState) return;
      chrome.scripting.executeScript({ target: { tabId: details.tabId }, files: ["content/click-tracker.js"] })
        .then(() => console.log("[Autopilot] Click tracker re-injected after navigation"))
        .catch((e) => console.warn("[Autopilot] Failed to re-inject click tracker:", e.message));
    }, 500);
  }

  // Re-inject voice recognizer on navigation when narration capture is active
  if (narrationCaptureState?.active && details.tabId === narrationCaptureState.tabId) {
    setTimeout(() => {
      if (!narrationCaptureState?.active) return;
      chrome.scripting.executeScript({ target: { tabId: details.tabId }, files: ["content/voice-recognizer.js"] })
        .then(() => console.log("[Narration] Voice recognizer re-injected after navigation"))
        .catch((e) => console.warn("[Narration] Failed to re-inject after navigation:", e.message));
    }, 1000);
  }
});

// ── CDP (Chrome DevTools Protocol) Session Manager ────────────────────────
// Uses chrome.debugger to send OS-level input events that work with React,
// Vue, Angular, and any framework — the same mechanism Playwright uses.

let _cdpAttachedTabId = null;

async function ensureCdpAttached(tabId) {
  if (_cdpAttachedTabId === tabId) return;
  // Detach from previous tab if needed
  if (_cdpAttachedTabId !== null) {
    try { await chrome.debugger.detach({ tabId: _cdpAttachedTabId }); } catch (_) {}
    _cdpAttachedTabId = null;
  }
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    _cdpAttachedTabId = tabId;
    console.log("[CDP] Attached to tab", tabId);
  } catch (err) {
    if (err.message?.includes("Already attached")) {
      _cdpAttachedTabId = tabId;
      console.log("[CDP] Already attached to tab", tabId);
    } else {
      throw new Error(`CDP attach failed: ${err.message}`);
    }
  }
}

async function cdpSend(tabId, method, params = {}) {
  await ensureCdpAttached(tabId);
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

// Clean up on debugger detach (e.g. user closes DevTools or navigates cross-origin)
chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId === _cdpAttachedTabId) {
    console.log("[CDP] Detached from tab", source.tabId, "reason:", reason);
    _cdpAttachedTabId = null;
  }
});

// ── Browser Command Queue: polling + execution ────────────────────────────

let _cmdPollTimer = null;
const CMD_POLL_INTERVAL = 500; // ms

function startCommandPolling() {
  if (_cmdPollTimer) return;
  _pollCommands(); // immediate first poll
  _cmdPollTimer = setInterval(_pollCommands, CMD_POLL_INTERVAL);
  console.log("[BrowserCmd] Polling started");
}

async function _pollCommands() {
  try {
    const commands = await backendFetch("GET", "/browser-commands/pending");
    if (!commands || commands.length === 0) return;

    for (const cmd of commands) {
      console.log("[BrowserCmd] Received:", cmd.command_type, cmd.id);
      _executeCommand(cmd)
        .then((data) => {
          return backendFetch("POST", `/browser-commands/${cmd.id}/result`, {
            success: true,
            data,
          });
        })
        .catch((err) => {
          console.warn("[BrowserCmd] Error executing", cmd.command_type, ":", err.message);
          return backendFetch("POST", `/browser-commands/${cmd.id}/result`, {
            success: false,
            error: err.message,
          });
        })
        .catch((postErr) => {
          console.warn("[BrowserCmd] Failed to post result:", postErr.message);
        });
    }
  } catch (_) {
    // Backend unreachable — silently continue polling
  }
}

async function _executeCommand(cmd) {
  const handler = _commandHandlers[cmd.command_type];
  if (!handler) throw new Error(`Unknown command type: ${cmd.command_type}`);
  return handler(cmd.params);
}

const _commandHandlers = {
  // ── Autopilot capture lifecycle (delegates to message handlers) ────────

  start_har_capture: async ({ captureSessionId }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    return handlers.START_HAR_CAPTURE({ captureSessionId, tabId });
  },

  stop_har_capture: async ({ captureSessionId }) => {
    return handlers.STOP_HAR_CAPTURE({ captureSessionId });
  },

  start_capture_session: async ({ projectId, processId, captureSessionId }) => {
    return handlers.START_CAPTURE_SESSION({ projectId, processId, captureSessionId });
  },

  stop_capture_session: async ({ captureSessionId }) => {
    return handlers.STOP_CAPTURE_SESSION({ captureSessionId });
  },

  inject_click_tracker: async () => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    return handlers.INJECT_CLICK_TRACKER({ tabId });
  },

  set_capture_state: async ({ projectId, processId, captureSessionId }) => {
    return handlers.SET_CAPTURE_STATE({ projectId, processId, captureSessionId });
  },

  start_url_monitoring: async ({ projectId, processId, captureSessionId }) => {
    return handlers.START_URL_MONITORING({ projectId, processId, captureSessionId });
  },

  // ── Original commands ─────────────────────────────────────────────────

  get_page_info: async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error("No active tab found");
    return {
      url: tab.url || "",
      title: tab.title || "",
      favIconUrl: tab.favIconUrl || "",
      tabId: tab.id,
    };
  },

  query_elements: async ({ selector, includeText, maxResults }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, inclText, maxRes) => {
        function deepQueryAll(root, s) {
          let results = [...root.querySelectorAll(s)];
          for (const el of root.querySelectorAll("*")) {
            if (el.shadowRoot) results.push(...deepQueryAll(el.shadowRoot, s));
          }
          return results;
        }
        const els = deepQueryAll(document, sel);
        const out = [];
        const limit = Math.min(els.length, maxRes || 20);
        for (let i = 0; i < limit; i++) {
          const el = els[i];
          const rect = el.getBoundingClientRect();
          const entry = {
            index: i,
            tagName: el.tagName.toLowerCase(),
            id: el.id || null,
            className: (typeof el.className === "string" ? el.className : "") || null,
            type: el.type || null,
            name: el.name || null,
            href: el.href || null,
            value: el.value !== undefined ? String(el.value).slice(0, 200) : null,
            placeholder: el.placeholder || null,
            disabled: el.disabled || false,
            visible: rect.width > 0 && rect.height > 0,
            rect: {
              x: Math.round(rect.x), y: Math.round(rect.y),
              width: Math.round(rect.width), height: Math.round(rect.height),
            },
          };
          if (inclText) {
            entry.textContent = (el.textContent || "").trim().slice(0, 200);
          }
          if (el.id) {
            entry.selector = `#${el.id}`;
          } else {
            let s = el.tagName.toLowerCase();
            if (el.className && typeof el.className === "string") {
              s += "." + el.className.trim().split(/\s+/).slice(0, 2).join(".");
            }
            entry.selector = s;
          }
          out.push(entry);
        }
        return { count: els.length, returned: out.length, elements: out };
      },
      args: [selector, includeText !== false, maxResults || 20],
    });

    return result.result;
  },

  click_element: async ({ selector }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { clicked: false, error: `No element found for selector: ${sel}` };
        el.click();
        return {
          clicked: true,
          tagName: el.tagName.toLowerCase(),
          id: el.id || null,
          textContent: (el.textContent || "").trim().slice(0, 100),
        };
      },
      args: [selector],
    });

    return result.result;
  },

  type_text: async ({ selector, text, clearFirst }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, txt, clear) => {
        const el = document.querySelector(sel);
        if (!el) return { typed: false, error: `No element found for selector: ${sel}` };

        el.focus();

        // Handle contenteditable elements
        if (el.isContentEditable) {
          if (clear) {
            el.textContent = "";
          }
          document.execCommand("insertText", false, txt);
          return {
            typed: true,
            tagName: el.tagName.toLowerCase(),
            id: el.id || null,
            finalValue: (el.textContent || "").slice(0, 200),
            method: "contenteditable",
          };
        }

        // React-compatible value setting via native setter
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;

        if (clear && nativeSetter) {
          nativeSetter.call(el, "");
          el.dispatchEvent(new Event("input", { bubbles: true }));
        } else if (clear) {
          el.value = "";
          el.dispatchEvent(new Event("input", { bubbles: true }));
        }

        if (nativeSetter) {
          nativeSetter.call(el, txt);
        } else {
          el.value = txt;
        }
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));

        return {
          typed: true,
          tagName: el.tagName.toLowerCase(),
          id: el.id || null,
          finalValue: el.value.slice(0, 200),
          method: nativeSetter ? "native_setter" : "direct",
        };
      },
      args: [selector, text, clearFirst !== false],
    });

    return result.result;
  },

  navigate: async ({ url }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    await chrome.tabs.update(tabId, { url });
    return { navigated: true, url, tabId };
  },

  eval_js: async ({ code }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (codeStr) => {
        try {
          const fn = new Function(codeStr);
          const val = fn();
          try {
            JSON.stringify(val);
            return { success: true, result: val };
          } catch (_) {
            return { success: true, result: String(val) };
          }
        } catch (err) {
          return { success: false, error: err.message };
        }
      },
      args: [code],
    });

    return result.result;
  },

  // ── Agent-friendly commands ──────────────────────────────────────────

  wait_for_selector: async ({ selector, timeout }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    const ms = timeout || 10000;

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, timeoutMs) => {
        return new Promise((resolve) => {
          if (document.querySelector(sel)) {
            resolve({ found: true, waited: 0 });
            return;
          }
          const start = Date.now();
          const observer = new MutationObserver(() => {
            if (document.querySelector(sel)) {
              observer.disconnect();
              resolve({ found: true, waited: Date.now() - start });
            }
          });
          observer.observe(document.body, { childList: true, subtree: true });
          setTimeout(() => {
            observer.disconnect();
            resolve({ found: !!document.querySelector(sel), waited: Date.now() - start, timeout: true });
          }, timeoutMs);
        });
      },
      args: [selector, ms],
    });

    return result.result;
  },

  wait_for_url: async ({ url_substring, timeout }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    const ms = timeout || 10000;

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sub, timeoutMs) => {
        return new Promise((resolve) => {
          if (window.location.href.includes(sub)) {
            resolve({ matched: true, url: window.location.href, waited: 0 });
            return;
          }
          const start = Date.now();
          const interval = setInterval(() => {
            if (window.location.href.includes(sub)) {
              clearInterval(interval);
              resolve({ matched: true, url: window.location.href, waited: Date.now() - start });
            } else if (Date.now() - start > timeoutMs) {
              clearInterval(interval);
              resolve({ matched: false, url: window.location.href, waited: Date.now() - start, timeout: true });
            }
          }, 200);
        });
      },
      args: [url_substring, ms],
    });

    return result.result;
  },

  get_page_summary: async () => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    // Run in all frames (main + iframes) and aggregate results
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        // Shadow DOM-piercing querySelectorAll
        function deepQueryAll(root, sel) {
          let results = [...root.querySelectorAll(sel)];
          for (const el of root.querySelectorAll("*")) {
            if (el.shadowRoot) results.push(...deepQueryAll(el.shadowRoot, sel));
          }
          return results;
        }

        const selectors = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [role="combobox"], [role="searchbox"]';
        const els = deepQueryAll(document, selectors);
        const vw = window.innerWidth, vh = window.innerHeight;

        // Separate visible-in-viewport vs offscreen, skip truly hidden
        const inView = [], offScreen = [];
        for (const el of els) {
          const rect = el.getBoundingClientRect();
          if (rect.width === 0 && rect.height === 0) continue;
          const style = getComputedStyle(el);
          if (style.display === "none" || style.visibility === "hidden") continue;
          if (rect.bottom >= 0 && rect.top <= vh && rect.right >= 0 && rect.left <= vw) {
            inView.push(el);
          } else {
            offScreen.push(el);
          }
        }

        // Prioritize in-viewport elements, then offscreen, cap at 200
        const combined = [...inView, ...offScreen];
        const limit = Math.min(combined.length, 200);
        const out = [];

        function buildSelector(el) {
          if (el.id) return `#${el.id}`;
          if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
          let s = el.tagName.toLowerCase();
          if (el.className && typeof el.className === "string") {
            s += "." + el.className.trim().split(/\s+/).slice(0, 2).join(".");
          }
          return s;
        }

        for (let i = 0; i < limit; i++) {
          const el = combined[i];
          out.push({
            index: i,
            tagName: el.tagName.toLowerCase(),
            type: el.type || null,
            id: el.id || null,
            name: el.name || null,
            text: (el.textContent || el.value || "").trim().slice(0, 100),
            placeholder: el.placeholder || null,
            ariaLabel: el.getAttribute("aria-label") || null,
            href: el.href || null,
            disabled: el.disabled || false,
            inViewport: i < inView.length,
            selector: buildSelector(el),
          });
        }
        return { total: els.length, inViewport: inView.length, returned: out.length, elements: out, isMainFrame: window === window.top };
      },
    });

    // Aggregate: main frame first, then iframe elements
    let allElements = [];
    let totalCount = 0;
    let inViewportCount = 0;
    for (const frame of results) {
      if (!frame.result) continue;
      const r = frame.result;
      totalCount += r.total;
      inViewportCount += r.inViewport;
      const framePrefix = r.isMainFrame ? "" : "[iframe] ";
      for (const el of r.elements) {
        el.text = framePrefix + el.text;
        allElements.push(el);
      }
    }

    // Re-index and cap at 200 total
    allElements = allElements.slice(0, 200);
    allElements.forEach((el, i) => el.index = i);

    return {
      url: tab?.url || "",
      title: tab?.title || "",
      total: totalCount,
      inViewport: inViewportCount,
      returned: allElements.length,
      elements: allElements,
    };
  },

  click_by_text: async ({ text }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (searchText) => {
        function deepQueryAll(root, sel) {
          let results = [...root.querySelectorAll(sel)];
          for (const el of root.querySelectorAll("*")) {
            if (el.shadowRoot) results.push(...deepQueryAll(el.shadowRoot, sel));
          }
          return results;
        }
        const lower = searchText.toLowerCase();
        const all = deepQueryAll(document, 'a, button, [role="button"], [role="link"], [role="tab"], [role="menuitem"], input[type="submit"], input[type="button"]');
        for (const el of all) {
          const elText = (el.textContent || el.value || "").trim().toLowerCase();
          const ariaLabel = (el.getAttribute("aria-label") || "").toLowerCase();
          if ((elText.includes(lower) || ariaLabel.includes(lower)) && el.offsetParent !== null) {
            el.click();
            return { clicked: true, tagName: el.tagName.toLowerCase(), text: (el.textContent || el.value || "").trim().slice(0, 100) };
          }
        }
        return { clicked: false, error: "No visible element found with text: " + searchText };
      },
      args: [text],
    });

    return result.result;
  },

  type_into_label: async ({ label, text }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (searchLabel, inputText) => {
        const lower = searchLabel.toLowerCase();
        const inputs = document.querySelectorAll("input, textarea, select");
        for (const el of inputs) {
          const ph = (el.placeholder || "").toLowerCase();
          const aria = (el.getAttribute("aria-label") || "").toLowerCase();
          const name = (el.name || "").toLowerCase();
          const id = el.id || "";
          let labelText = "";
          if (id) {
            const lbl = document.querySelector(`label[for="${id}"]`);
            if (lbl) labelText = (lbl.textContent || "").trim().toLowerCase();
          }
          if (ph.includes(lower) || aria.includes(lower) || name.includes(lower) || labelText.includes(lower)) {
            el.focus();
            el.value = inputText;
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            return { typed: true, tagName: el.tagName.toLowerCase(), name: el.name, id: el.id };
          }
        }
        return { typed: false, error: "No input found matching label: " + searchLabel };
      },
      args: [label, text],
    });

    return result.result;
  },

  select_option: async ({ selector, value }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, val) => {
        const selectEl = document.querySelector(sel);
        if (!selectEl) return { selected: false, error: "No select found for: " + sel };
        const lower = val.toLowerCase();
        for (const opt of selectEl.options) {
          if (opt.value === val || opt.text.toLowerCase().includes(lower)) {
            selectEl.value = opt.value;
            selectEl.dispatchEvent(new Event("change", { bubbles: true }));
            return { selected: true, value: opt.value, text: opt.text };
          }
        }
        return { selected: false, error: "Option not found: " + val };
      },
      args: [selector, value],
    });

    return result.result;
  },

  press_key: async ({ key, selector }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (keyName, sel) => {
        const target = sel ? document.querySelector(sel) : document.activeElement;
        if (!target) return { pressed: false, error: "No target element" };
        target.dispatchEvent(new KeyboardEvent("keydown", { key: keyName, bubbles: true }));
        target.dispatchEvent(new KeyboardEvent("keyup", { key: keyName, bubbles: true }));
        if (keyName === "Enter") {
          target.dispatchEvent(new KeyboardEvent("keypress", { key: keyName, bubbles: true }));
        }
        return { pressed: true, key: keyName, tagName: target.tagName.toLowerCase() };
      },
      args: [key, selector || ""],
    });

    return result.result;
  },

  take_screenshot: async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) throw new Error("No active tab found");

    const dataUrl = await chrome.tabs.captureVisibleTab(null, { format: "png" });
    const response = await fetch(dataUrl);
    const blob = await response.blob();

    const formData = new FormData();
    formData.append("file", blob, "browser-cmd-screenshot.png");
    formData.append("url", tab.url || "");

    const uploadResp = await fetch(`${BACKEND_URL}/screenshots`, {
      method: "POST",
      body: formData,
    });

    if (!uploadResp.ok) throw new Error(`Screenshot upload failed: ${uploadResp.status}`);

    const screenshotData = await uploadResp.json();
    return {
      screenshot_id: screenshotData.id,
      url: tab.url || "",
      image_url: `${BACKEND_URL}/screenshots/${screenshotData.id}/image`,
    };
  },

  // ── CDP-based commands (OS-level input, works with React/Vue/Angular) ───

  cdp_click: async ({ selector, x, y }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    let clickX = x;
    let clickY = y;

    if (selector && (clickX === undefined || clickY === undefined)) {
      // Resolve selector to coordinates via page script
      const [result] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
          const rect = el.getBoundingClientRect();
          return {
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2),
            tagName: el.tagName.toLowerCase(),
            text: (el.textContent || "").trim().slice(0, 100),
          };
        },
        args: [selector],
      });

      if (!result.result) {
        return { clicked: false, error: `No element found for selector: ${selector}` };
      }
      clickX = result.result.x;
      clickY = result.result.y;
    }

    if (clickX === undefined || clickY === undefined) {
      return { clicked: false, error: "No coordinates resolved — provide selector or x/y" };
    }

    // OS-level mouse events via CDP
    await cdpSend(tabId, "Input.dispatchMouseEvent", {
      type: "mousePressed", x: clickX, y: clickY, button: "left", clickCount: 1,
    });
    await cdpSend(tabId, "Input.dispatchMouseEvent", {
      type: "mouseReleased", x: clickX, y: clickY, button: "left", clickCount: 1,
    });

    return { clicked: true, x: clickX, y: clickY, selector: selector || null };
  },

  cdp_type: async ({ selector, text, clearFirst }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    // Focus the element by clicking it
    if (selector) {
      const [result] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
          const rect = el.getBoundingClientRect();
          return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) };
        },
        args: [selector],
      });

      if (!result.result) {
        return { typed: false, error: `No element found for selector: ${selector}` };
      }

      // Click to focus
      await cdpSend(tabId, "Input.dispatchMouseEvent", {
        type: "mousePressed", x: result.result.x, y: result.result.y, button: "left", clickCount: 1,
      });
      await cdpSend(tabId, "Input.dispatchMouseEvent", {
        type: "mouseReleased", x: result.result.x, y: result.result.y, button: "left", clickCount: 1,
      });
    }

    // Clear existing text if requested
    if (clearFirst) {
      // Select all (Ctrl+A)
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyDown", key: "a", code: "KeyA", modifiers: 2, // 2 = Ctrl
        windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
      });
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyUp", key: "a", code: "KeyA", modifiers: 2,
        windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65,
      });
      // Delete selection
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyDown", key: "Backspace", code: "Backspace",
        windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8,
      });
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyUp", key: "Backspace", code: "Backspace",
        windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 8,
      });
    }

    // Type each character via CDP — triggers React onChange, autocomplete, etc.
    for (const char of text) {
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyDown", text: char, key: char,
        windowsVirtualKeyCode: char.charCodeAt(0), nativeVirtualKeyCode: char.charCodeAt(0),
      });
      await cdpSend(tabId, "Input.dispatchKeyEvent", {
        type: "keyUp", key: char,
        windowsVirtualKeyCode: char.charCodeAt(0), nativeVirtualKeyCode: char.charCodeAt(0),
      });
    }

    return { typed: true, length: text.length, selector: selector || null };
  },

  cdp_get_accessibility_tree: async ({ maxDepth, interactiveOnly }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const { nodes } = await cdpSend(tabId, "Accessibility.getFullAXTree", {
      depth: maxDepth || 10,
    });

    // Interactive roles we care about for autopilot
    const interactiveRoles = new Set([
      "button", "link", "textbox", "combobox", "searchbox", "spinbutton",
      "slider", "checkbox", "radio", "switch", "tab", "menuitem",
      "menuitemcheckbox", "menuitemradio", "option", "listbox", "tree",
      "treeitem", "gridcell", "columnheader", "rowheader", "row",
    ]);

    const filterInteractive = interactiveOnly !== false;
    const elements = [];

    for (const node of nodes) {
      const role = node.role?.value || "";
      const name = node.name?.value || "";
      const value = node.value?.value || "";
      const description = node.description?.value || "";

      // Skip ignored/generic nodes
      if (node.ignored || role === "none" || role === "generic") continue;

      // Filter to interactive elements if requested
      if (filterInteractive && !interactiveRoles.has(role)) continue;

      // Skip unnamed elements (usually decorative)
      if (!name && !value && !description) continue;

      const entry = {
        nodeId: node.nodeId,
        role,
        name,
        value: value || null,
        description: description || null,
        focused: node.focused || false,
        disabled: false,
      };

      // Check for disabled state
      if (node.properties) {
        for (const prop of node.properties) {
          if (prop.name === "disabled" && prop.value?.value === true) {
            entry.disabled = true;
          }
        }
      }

      // Get bounding box if available via backend node
      if (node.backendDOMNodeId) {
        entry.backendDOMNodeId = node.backendDOMNodeId;
      }

      elements.push(entry);
    }

    // Get current page info
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    return {
      url: tab?.url || "",
      title: tab?.title || "",
      total: nodes.length,
      interactive: elements.length,
      elements,
    };
  },

  cdp_press_key: async ({ key, modifiers }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    // Map common key names to CDP parameters
    const keyMap = {
      "Enter": { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
      "Tab": { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
      "Escape": { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
      "Backspace": { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
      "ArrowDown": { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
      "ArrowUp": { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
      "ArrowLeft": { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
      "ArrowRight": { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 },
      "Space": { key: " ", code: "Space", windowsVirtualKeyCode: 32 },
      "Delete": { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 },
      "Home": { key: "Home", code: "Home", windowsVirtualKeyCode: 36 },
      "End": { key: "End", code: "End", windowsVirtualKeyCode: 35 },
      "PageUp": { key: "PageUp", code: "PageUp", windowsVirtualKeyCode: 33 },
      "PageDown": { key: "PageDown", code: "PageDown", windowsVirtualKeyCode: 34 },
    };

    const mapped = keyMap[key] || { key, code: key, windowsVirtualKeyCode: key.charCodeAt(0) };
    const mod = modifiers || 0; // 1=Alt, 2=Ctrl, 4=Meta, 8=Shift

    await cdpSend(tabId, "Input.dispatchKeyEvent", {
      type: "keyDown", ...mapped, modifiers: mod, nativeVirtualKeyCode: mapped.windowsVirtualKeyCode,
    });
    await cdpSend(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp", ...mapped, modifiers: mod, nativeVirtualKeyCode: mapped.windowsVirtualKeyCode,
    });

    return { pressed: true, key, modifiers: mod };
  },

  // ── Scroll commands ─────────────────────────────────────────────────────

  scroll_page: async ({ direction, amount }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    // Get viewport size and current scroll position
    const [info] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({
        vw: window.innerWidth,
        vh: window.innerHeight,
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
      }),
    });
    const { vw, vh } = info.result;

    // Calculate pixel delta
    let pixels;
    if (typeof amount === "number") {
      pixels = amount;
    } else if (amount === "half") {
      pixels = Math.round((direction === "left" || direction === "right" ? vw : vh) / 2);
    } else {
      // default: "page"
      pixels = direction === "left" || direction === "right" ? vw : vh;
    }

    const deltaX = direction === "right" ? pixels : direction === "left" ? -pixels : 0;
    const deltaY = direction === "down" ? pixels : direction === "up" ? -pixels : 0;

    // Move mouse to viewport center, then dispatch wheel event via CDP
    const cx = Math.round(vw / 2);
    const cy = Math.round(vh / 2);

    await cdpSend(tabId, "Input.dispatchMouseEvent", {
      type: "mouseMoved", x: cx, y: cy,
    });
    await cdpSend(tabId, "Input.dispatchMouseEvent", {
      type: "mouseWheel", x: cx, y: cy, deltaX, deltaY,
    });

    // Wait for scroll to settle and lazy-load to trigger
    await new Promise((r) => setTimeout(r, 350));

    // Get new scroll position
    const [after] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({
        scrollX: window.scrollX,
        scrollY: window.scrollY,
        scrollWidth: document.documentElement.scrollWidth,
        scrollHeight: document.documentElement.scrollHeight,
        atTop: window.scrollY === 0,
        atBottom: window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1,
      }),
    });

    return { scrolled: true, direction, deltaX, deltaY, ...after.result };
  },

  scroll_element: async ({ selector, direction, amount }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, dir, amt) => {
        function deepQuery(root, s) {
          const el = root.querySelector(s);
          if (el) return el;
          for (const node of root.querySelectorAll("*")) {
            if (node.shadowRoot) {
              const found = deepQuery(node.shadowRoot, s);
              if (found) return found;
            }
          }
          return null;
        }

        const el = deepQuery(document, sel);
        if (!el) return { scrolled: false, error: `No element found for selector: ${sel}` };

        const isHorizontal = dir === "left" || dir === "right";
        const containerSize = isHorizontal ? el.clientWidth : el.clientHeight;

        let pixels;
        if (typeof amt === "number") {
          pixels = amt;
        } else if (amt === "half") {
          pixels = Math.round(containerSize / 2);
        } else {
          pixels = containerSize; // default: "page"
        }

        const top = dir === "down" ? pixels : dir === "up" ? -pixels : 0;
        const left = dir === "right" ? pixels : dir === "left" ? -pixels : 0;

        el.scrollBy({ top, left, behavior: "smooth" });
        el.dispatchEvent(new Event("scroll", { bubbles: true }));

        return {
          scrolled: true,
          selector: sel,
          direction: dir,
          scrollTop: el.scrollTop,
          scrollLeft: el.scrollLeft,
          scrollHeight: el.scrollHeight,
          scrollWidth: el.scrollWidth,
          clientHeight: el.clientHeight,
          clientWidth: el.clientWidth,
        };
      },
      args: [selector, direction, amount],
    });

    return result.result;
  },

  scroll_to_element: async ({ selector, block }) => {
    const tabId = await getActiveTabId();
    if (!tabId) throw new Error("No active tab found");

    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel, blk) => {
        function deepQuery(root, s) {
          const el = root.querySelector(s);
          if (el) return el;
          for (const node of root.querySelectorAll("*")) {
            if (node.shadowRoot) {
              const found = deepQuery(node.shadowRoot, s);
              if (found) return found;
            }
          }
          return null;
        }

        const el = deepQuery(document, sel);
        if (!el) return { scrolled: false, error: `No element found for selector: ${sel}` };

        el.scrollIntoView({ block: blk || "center", inline: "nearest", behavior: "smooth" });

        const rect = el.getBoundingClientRect();
        return {
          scrolled: true,
          selector: sel,
          tagName: el.tagName.toLowerCase(),
          text: (el.textContent || "").trim().slice(0, 100),
          rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
        };
      },
      args: [selector, block || "center"],
    });

    return result.result;
  },
};

// Start polling when service worker loads
startCommandPolling();

// ── Side Panel setup ───────────────────────────────────────────────────────

if (chrome.sidePanel) {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })
    .catch((err) => console.warn("[Elicitation Agent] Side panel setup failed:", err));
}

console.log("[Elicitation Agent] Service worker started");
