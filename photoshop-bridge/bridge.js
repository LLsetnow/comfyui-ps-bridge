"use strict";

const { entrypoints } = require("uxp");
const API_ROOT = "/ps-bridge/v1";
const DEFAULT_ADDRESS = "127.0.0.1:8188";
const LONG_POLL_SECONDS = 10;
const LONG_POLL_TIMEOUT_MS = 18000;
const sessionId = "ps-" + Date.now() + "-" + Math.random().toString(36).slice(2);
const sessionQuery = "?session_id=" + encodeURIComponent(sessionId);
const completedJobs = new Map();
const ui = {
  address: document.getElementById("address"),
  connect: document.getElementById("connect"),
  capture: document.getElementById("capture"),
  snapshot: document.getElementById("snapshot"),
  status: document.getElementById("status")
};

let connection = null;
let visible = false;
let busy = false;
let polling = false;
let pollFailed = false;
let pollTimer = null;
let pollAbortController = null;

function status(message, state = "") {
  ui.status.textContent = message;
  ui.status.setAttribute("data-state", state);
}

function renderControls() {
  ui.connect.textContent = connection ? "断开" : "连接";
  ui.connect.disabled = busy;
  ui.capture.disabled = busy || !connection;
  if (busy || connection) {
    ui.address.setAttribute("disabled", "");
  } else {
    ui.address.removeAttribute("disabled");
  }
}

async function request(current, path, { body, binary = false, timeout = 8000, signal = null } = {}) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  if (signal && signal.aborted) {
    const aborted = new Error("Aborted");
    aborted.name = "AbortError";
    throw aborted;
  }
  const onAbort = controller && signal ? () => controller.abort() : null;
  if (onAbort) signal.addEventListener("abort", onAbort, { once: true });
  const timer = controller && timeout > 0 ? setTimeout(() => controller.abort(), timeout) : null;
  const headers = {};
  let payload;
  if (body instanceof Uint8Array) {
    headers["Content-Type"] = "application/octet-stream";
    payload = body;
  } else if (body !== undefined && body !== null) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  try {
    const response = await fetch(current.endpoint + API_ROOT + path, {
      method: payload !== undefined ? "POST" : "GET",
      headers,
      credentials: "omit",
      redirect: "error",
      ...(payload !== undefined ? { body: payload } : {}),
      ...(controller ? { signal: controller.signal } : {})
    });
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch (_) {}
      const error = new Error(String(detail.error || detail.message || "桥接请求失败（HTTP " + response.status + "）"));
      error.httpStatus = response.status;
      throw error;
    }
    return binary ? new Uint8Array(await response.arrayBuffer()) : await response.json();
  } finally {
    if (timer) clearTimeout(timer);
    if (onAbort) signal.removeEventListener("abort", onAbort);
  }
}

function parseAddress(value) {
  let trimmed = String(value || "").trim().replace(/\/+$/, "");
  if (!trimmed) throw new Error("请输入 ComfyUI 地址，例如 127.0.0.1:8188");
  if (/^\d{1,5}$/.test(trimmed)) trimmed = "127.0.0.1:" + trimmed;
  const hasScheme = /^https?:\/\//i.test(trimmed);
  const rawUrl = hasScheme ? trimmed : "http://" + trimmed;
  const match = /^(https?):\/\/(\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+):(\d{1,5})$/.exec(rawUrl);
  if (!match) {
    throw new Error("地址格式应为 IP:端口，例如 127.0.0.1:8188 或 192.168.1.10:8188");
  }
  const scheme = match[1].toLowerCase();
  const host = match[2];
  const port = Number(match[3]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("端口范围应为 1–65535");
  }
  const hostPort = host + ":" + port;
  const endpoint = scheme + "://" + hostPort;
  const display = scheme === "https" ? endpoint : hostPort;
  return { display, endpoint };
}

function restorePreferences() {
  ui.address.value = DEFAULT_ADDRESS;
  try {
    const saved = localStorage.getItem("address") || localStorage.getItem("endpoint") || localStorage.getItem("port");
    if (saved) ui.address.value = parseAddress(saved).display;
  } catch (_) {
    // Preference storage is optional. Manual connection is still available.
  }
}

function notifyDisconnect(current) {
  if (!current || !current.supportsLongPoll) return;
  void request(current, "/sessions/bye", { body: { session_id: sessionId }, timeout: 1500 }).catch(() => {});
}

async function toggleConnection() {
  if (busy) return;
  if (connection) {
    const previous = connection;
    connection = null;
    stopPolling();
    notifyDisconnect(previous);
    status("已断开");
    renderControls();
    return;
  }
  busy = true;
  renderControls();
  status("正在连接 ComfyUI…");
  try {
    const current = parseAddress(ui.address.value);
    ui.address.value = current.display;
    try {
      localStorage.setItem("address", current.display);
    } catch (_) {
      // Keep the current value even if preference storage is unavailable.
    }
    const info = await request(current, "/health");
    const capabilities = new Set(Array.isArray(info && info.capabilities) ? info.capabilities : []);
    current.supportsBinaryCapture = capabilities.has("binary_capture_v1");
    current.supportsJobBundle = capabilities.has("job_bundle_v1");
    current.supportsLongPoll = capabilities.has("long_poll_v1");
    connection = current;
    pollFailed = false;
    ui.snapshot.textContent = "";
    status("已连接 · " + current.display, "good");
  } catch (error) {
    status("连接失败 · " + error.message, "error");
  } finally {
    busy = false;
    renderControls();
    schedulePoll(0);
  }
}

function bytesToBase64(bytes) {
  // Chunk size is divisible by three so only the final chunk needs padding.
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 0x3000) {
    chunks.push(btoa(String.fromCharCode(...bytes.subarray(offset, offset + 0x3000))));
  }
  return chunks.join("");
}

async function captureCurrentLayer(requestId = null) {
  if (busy || !connection) return;
  const current = connection;
  busy = true;
  stopPolling();
  renderControls();
  status("正在捕获 Photoshop 图层…");
  try {
    if (current.supportsBinaryCapture) {
      const snapshot = await capturePhotoshop(sessionId, { selectionOnly: true, imageChannels: 3 });
      const encodedAssets = {};
      await Promise.all(Object.entries(snapshot.assets).map(async ([mode, asset]) => {
        encodedAssets[mode] = asset ? await encodeBinaryAsset(asset) : null;
      }));
      if (requestId) snapshot.request_id = requestId;
      const packet = buildCapturePacket(snapshot, encodedAssets);
      await request(current, "/captures", { body: packet, timeout: 120000 });
      const selection = snapshot.selection_rect;
      const rect = selection || snapshot.layer_rect;
      const dimensions = rectWidth(rect) + " × " + rectHeight(rect);
      ui.snapshot.textContent = snapshot.layer_name + " · " + (selection ? "选区 " : "图层 ") + dimensions;
    } else {
      const snapshot = await capturePhotoshop(sessionId, { selectionOnly: false, imageChannels: 4 });
      const assets = {};
      for (const [mode, asset] of Object.entries(snapshot.assets)) {
        assets[mode] = asset ? {
          rect: asset.rect,
          png: bytesToBase64(_encodePng(asset.pixels, rectWidth(asset.rect), rectHeight(asset.rect), asset.channels))
        } : null;
      }
      const body = { ...snapshot, assets };
      if (requestId) body.request_id = requestId;
      await request(current, "/captures", { body, timeout: 120000 });
      const selection = snapshot.selection_rect;
      const rect = selection || snapshot.layer_rect;
      const dimensions = rectWidth(rect) + " × " + rectHeight(rect);
      ui.snapshot.textContent = snapshot.layer_name + " · " + (selection ? "选区 " : "图层 ") + dimensions;
    }
    status("已捕获 · 可在 ComfyUI 运行工作流", "good");
  } catch (error) {
    ui.snapshot.textContent = "";
    status("捕获失败 · " + error.message, "error");
    if (requestId) {
      try {
        await request(current, "/capture-requests/" + encodeURIComponent(requestId) + "/error", {
          body: { session_id: sessionId, error: String(error.message || error).slice(0, 2048) }
        });
      } catch (_) {
        // A pending request can be retried by polling until it expires.
      }
    }
  } finally {
    busy = false;
    renderControls();
    schedulePoll(0);
  }
}

function stopPolling() {
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  if (pollAbortController) {
    const controller = pollAbortController;
    pollAbortController = null;
    controller.abort();
  }
}

function schedulePoll(delay = 1600) {
  if (!visible || !connection || polling) return;
  if (pollTimer !== null) {
    if (delay > 0) return;
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  pollTimer = setTimeout(poll, delay);
}

function validateJob(job) {
  if (job.session_id !== sessionId) throw new Error("回贴任务属于另一个 Photoshop 会话");
  if (typeof job.document_id !== "string" || !job.document_id ||
      !Number.isSafeInteger(job.left) || !Number.isSafeInteger(job.top) ||
      !Number.isSafeInteger(job.width) || !Number.isSafeInteger(job.height) ||
      job.width < 1 || job.height < 1 || job.width * job.height > MAX_PIXELS) {
    throw new Error("回贴任务的坐标或尺寸无效");
  }
}

async function applyJob(current, job) {
  validateJob(job);
  const route = "/jobs/" + encodeURIComponent(job.id);
  let layerId = completedJobs.get(job.id);
  if (!layerId) {
    let targetError;
    try {
      assertTargetDocument(job);
    } catch (error) {
      targetError = error;
    }
    if (!targetError) {
      const size = job.width * job.height;
      const expectedChannels = job.channels === 3 ? 3 : 4;
      let pixels;
      let mask;
      if (current.supportsJobBundle) {
        const bundle = await request(current, route + "/bundle" + sessionQuery, { binary: true, timeout: 120000 });
        if (!visible || current !== connection) return;
        const decoded = parseDeliveryBundle(bundle, job.width, job.height);
        pixels = decoded.pixels;
        mask = decoded.mask;
      } else {
        const [rawPixels, rawMask] = await Promise.all([
          request(current, route + "/pixels" + sessionQuery, { binary: true, timeout: 120000 }),
          request(current, route + "/mask" + sessionQuery, { binary: true, timeout: 120000 })
        ]);
        if (!visible || current !== connection) return;
        if (rawPixels.length !== size * expectedChannels || rawMask.length !== size) {
          throw new Error("回贴的图像或蒙版数据长度不符");
        }
        pixels = rawPixels;
        mask = rawMask;
      }
      try {
        layerId = await importPhotoshop(job, pixels, mask);
      } catch (error) {
        // Modal collisions and cancellation can be retried without a layer.
        if (Number(error.number) === 9 || Number(error.number) === -128) throw error;
        targetError = error;
      }
    }
    if (targetError) {
      await request(current, route + "/error", { body: { session_id: sessionId, error: targetError.message } });
      status("回贴失败 · " + targetError.message, "error");
      return;
    }
    // Keep the written layer ID until ComfyUI confirms delivery. A lost ACK
    // retries the HTTP operation without applying the same result twice.
    completedJobs.set(job.id, layerId);
  }
  await request(current, route + "/complete", { body: { session_id: sessionId, layer_id: layerId } });
  completedJobs.delete(job.id);
  status("已回贴带蒙版图层 · " + (job.layer_name || "ComfyUI"), "good");
}

async function poll() {
  pollTimer = null;
  if (!visible || !connection || busy) {
    if (!busy) schedulePoll();
    return;
  }
  const current = connection;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  pollAbortController = controller;
  polling = true;
  let fastEmptyReturn = false;
  try {
    const startedAt = Date.now();
    const pollQuery = current.supportsLongPoll
      ? "/jobs/pending" + sessionQuery + "&capture_requests=1&wait=" + LONG_POLL_SECONDS
      : "/jobs/pending" + sessionQuery + "&capture_requests=1";
    const { job, capture_request: captureRequest } = await request(current, pollQuery, {
      timeout: current.supportsLongPoll ? LONG_POLL_TIMEOUT_MS : 8000,
      signal: controller ? controller.signal : null
    });
    if (!visible || connection !== current || busy) return;
    if (pollFailed) status("连接已恢复 · " + current.display, "good");
    pollFailed = false;
    if (captureRequest) {
      await captureCurrentLayer(captureRequest.id);
      return;
    }
    if (!job) {
      fastEmptyReturn = current.supportsLongPoll && Date.now() - startedAt < 250;
      return;
    }
    busy = true;
    renderControls();
    status("正在回贴 ComfyUI 结果…");
    try {
      await applyJob(current, job);
    } finally {
      busy = false;
      renderControls();
    }
  } catch (error) {
    if (controller && controller.signal.aborted) return;
    if (connection !== current || !visible || busy) return;
    pollFailed = true;
    status("等待回贴 · " + error.message, "error");
  } finally {
    if (pollAbortController === controller) pollAbortController = null;
    polling = false;
    if (!busy) {
      const nextDelay = pollFailed ? 3500 : (current.supportsLongPoll ? (fastEmptyReturn ? 250 : 0) : undefined);
      schedulePoll(nextDelay);
    }
  }
}

ui.connect.addEventListener("click", toggleConnection);
ui.capture.addEventListener("click", () => { void captureCurrentLayer(); });
restorePreferences();
entrypoints.setup({
  panels: {
    comfyUiPhotoshopBridgePanel: {
      show() {
        visible = true;
        schedulePoll(0);
      },
      hide() {
        visible = false;
        stopPolling();
        notifyDisconnect(connection);
      },
      destroy() {
        visible = false;
        stopPolling();
        notifyDisconnect(connection);
      }
    }
  }
});
