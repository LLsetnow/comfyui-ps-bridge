"use strict";

// UXP imaging flow adapted from rh-workflow-ps. Photoshop reads and writes
// stay inside modal scopes. PNG encoding and HTTP run outside those scopes.
const { app, core, imaging, action, constants } = require("photoshop");
const SRGB = "sRGB IEC61966-2.1";
const GRAY = "Gray Gamma 2.2";
const MAX_PIXELS = 32 * 1024 * 1024;

function pixelValue(value) {
  if (value && typeof value._value === "number") return value._value;
  return Number(value);
}

function pixelRect(bounds, canvas) {
  if (!bounds) return null;
  let left = Math.floor(pixelValue(bounds.left));
  let top = Math.floor(pixelValue(bounds.top));
  let right = Math.ceil(pixelValue(bounds.right));
  let bottom = Math.ceil(pixelValue(bounds.bottom));
  if (canvas) {
    left = Math.max(0, left);
    top = Math.max(0, top);
    right = Math.min(canvas.width, right);
    bottom = Math.min(canvas.height, bottom);
  }
  if (!(right > left && bottom > top)) return null;
  return { left, top, right, bottom };
}

function rectWidth(rect) {
  return rect.right - rect.left;
}

function rectHeight(rect) {
  return rect.bottom - rect.top;
}

function checkCaptureSize(rect) {
  if (rect && rectWidth(rect) * rectHeight(rect) > MAX_PIXELS) {
    throw new Error("捕获范围超过 3200 万像素，请缩小图层或选区");
  }
}

function componentSize(doc) {
  const depth = String(doc.bitsPerChannel);
  if (depth === "bitDepth16" || depth === "16") return 16;
  if (depth === "bitDepth32" || depth === "32") return 32;
  return 8;
}

function normalize8(data, depth, components, color) {
  if (data instanceof Uint8Array) return data;
  const scale = depth === 16 ? 255 / 32768 : 255;
  return Uint8Array.from(data, (sample, index) => {
    if (depth === 32 && color && index % components < 3) {
      sample = sample <= 0.0031308 ? sample * 12.92 : 1.055 * Math.pow(sample, 1 / 2.4) - 0.055;
    }
    return Math.min(255, Math.max(0, Math.round(sample * scale)));
  });
}

function reframe(pixels, source, target, inChannels, outChannels) {
  const width = rectWidth(target);
  const height = rectHeight(target);
  const sourceWidth = rectWidth(source);
  const output = new Uint8Array(width * height * outChannels);
  const left = Math.max(source.left, target.left);
  const right = Math.min(source.right, target.right);
  const top = Math.max(source.top, target.top);
  const bottom = Math.min(source.bottom, target.bottom);
  if (right <= left || bottom <= top) return output;
  for (let y = top; y < bottom; y++) {
    let read = ((y - source.top) * sourceWidth + left - source.left) * inChannels;
    let write = ((y - target.top) * width + left - target.left) * outChannels;
    if (inChannels === outChannels) {
      output.set(pixels.subarray(read, read + (right - left) * inChannels), write);
      continue;
    }
    for (let x = left; x < right; x++) {
      output[write] = pixels[read];
      if (outChannels === 4) {
        output[write + 1] = inChannels >= 3 ? pixels[read + 1] : pixels[read];
        output[write + 2] = inChannels >= 3 ? pixels[read + 2] : pixels[read];
        output[write + 3] = inChannels === 2 ? pixels[read + 1] : 255;
      }
      read += inChannels;
      write += outChannels;
    }
  }
  return output;
}

async function readPixels(result, rect, channels) {
  const image = result.imageData;
  try {
    const source = {
      left: pixelValue(result.sourceBounds.left),
      top: pixelValue(result.sourceBounds.top),
      right: pixelValue(result.sourceBounds.left) + image.width,
      bottom: pixelValue(result.sourceBounds.top) + image.height
    };
    const pixels = normalize8(await image.getData({ chunky: true }), image.componentSize, image.components, channels === 4);
    return reframe(pixels, source, rect, image.components, channels);
  } finally {
    image.dispose();
  }
}

async function capturePhotoshop(sessionId) {
  return core.executeAsModal(async () => {
    const doc = app.documents.length ? app.activeDocument : null;
    if (!doc) throw new Error("请打开 Photoshop 文档");
    if (doc.activeLayers.length !== 1) throw new Error("请选择一个 Photoshop 图层");
    const layer = doc.activeLayers[0];
    const canvas = { width: pixelValue(doc.width), height: pixelValue(doc.height) };
    const [properties] = await action.batchPlay([{
      _obj: "get",
      _target: [{ _ref: "layer", _id: layer.id }, { _ref: "document", _id: doc.id }],
      _options: { dialogOptions: "dontDisplay" }
    }], {});
    if (!properties || properties._obj === "error") throw new Error("无法读取所选图层");

    const layerRect = pixelRect(properties.boundsNoMask || layer.boundsNoEffects);
    if (!layerRect) throw new Error("当前图层没有可捕获的像素");
    const selectionRect = pixelRect(doc.selection && doc.selection.bounds, canvas);
    checkCaptureSize(layerRect);
    checkCaptureSize(selectionRect);
    const layerOptions = { documentID: doc.id, layerID: layer.id };
    const layerPixels = await readPixels(await imaging.getPixels({
      ...layerOptions,
      sourceBounds: layerRect,
      colorSpace: "RGB",
      colorProfile: SRGB,
      componentSize: componentSize(doc),
      applyAlpha: false
    }), layerRect, 4);
    const layerMask = properties.hasUserMask ? await readPixels(await imaging.getLayerMask({
      ...layerOptions, kind: "user", sourceBounds: layerRect
    }), layerRect, 1) : null;
    const selectionMask = selectionRect ? await readPixels(await imaging.getSelection({
      documentID: doc.id, sourceBounds: selectionRect
    }), selectionRect, 1) : null;
    return {
      session_id: sessionId,
      document_id: String(doc.id),
      document_name: String(doc.title || doc.name || ""),
      document_width: canvas.width,
      document_height: canvas.height,
      layer_id: String(layer.id),
      layer_name: String(layer.name || ""),
      layer_rect: layerRect,
      selection_rect: selectionRect,
      assets: {
        layer_image: { rect: layerRect, pixels: layerPixels, channels: 4 },
        layer_mask: layerMask ? { rect: layerRect, pixels: layerMask, channels: 1 } : null,
        selection_image: selectionRect ? {
          rect: selectionRect,
          pixels: reframe(layerPixels, layerRect, selectionRect, 4, 4),
          channels: 4
        } : null,
        selection_mask: selectionMask ? { rect: selectionRect, pixels: selectionMask, channels: 1 } : null
      }
    };
  }, { commandName: "ComfyUI · 捕获图层", timeOut: 1000 });
}

function findDocument(id) {
  return Array.from(app.documents).find(doc => String(doc.id) === id);
}

function assertTargetDocument(job) {
  const doc = findDocument(job.document_id);
  if (!doc) throw new Error("原 Photoshop 文档已关闭");
  if (pixelValue(doc.width) !== job.document_width || pixelValue(doc.height) !== job.document_height) {
    throw new Error("原文档的画布尺寸已变化，请重新捕获后运行");
  }
  return doc;
}

function srgbToLinear(value) {
  return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
}

async function writeResult(doc, layer, job, rgba, mask) {
  const depth = componentSize(doc);
  let buffer = rgba;
  // Photoshop expects native-depth RGBA to preserve alpha when writing pixels.
  if (depth === 16) buffer = Uint16Array.from(rgba, sample => Math.round(sample * 32768 / 255));
  if (depth === 32) buffer = Float32Array.from(rgba, (sample, index) => index % 4 === 3 ? sample / 255 : srgbToLinear(sample / 255));

  const scope = {
    documentID: doc.id, layerID: layer.id,
    targetBounds: { left: job.left, top: job.top }
  };
  const dimensions = { width: job.width, height: job.height, chunky: true };
  const image = await imaging.createImageDataFromBuffer(buffer, {
    ...dimensions, components: 4, colorSpace: "RGB", colorProfile: SRGB, fullRange: false
  });
  try {
    await imaging.putPixels({ ...scope, imageData: image, replace: true });
  } finally {
    image.dispose();
  }

  doc.activeLayers = [layer];
  const [result] = await action.batchPlay([{
    _obj: "make",
    new: { _class: "channel" },
    at: { _ref: "channel", _enum: "channel", _value: "mask" },
    using: { _enum: "userMaskEnabled", _value: "hideAll" },
    _options: { dialogOptions: "dontDisplay" }
  }], {});
  if (result && result._obj === "error") throw new Error(result.message || "无法创建图层蒙版");
  // Pixels outside the captured rectangle stay black. Selection alpha is kept
  // separate from image alpha so the returned layer remains editable.
  const gray = await imaging.createImageDataFromBuffer(mask, {
    ...dimensions, components: 1, colorSpace: "Grayscale", colorProfile: GRAY
  });
  try {
    await imaging.putLayerMask({ ...scope, kind: "user", imageData: gray, replace: false });
  } finally {
    gray.dispose();
  }
}

async function importPhotoshop(job, rgba, mask) {
  return core.executeAsModal(async ({ hostControl }) => {
    const doc = assertTargetDocument(job);
    const previousDoc = app.activeDocument;
    const previousLayers = Array.from(doc.activeLayers);
    const history = await hostControl.suspendHistory({
      documentID: doc.id, name: "ComfyUI · 回贴带蒙版图层"
    });
    let committed = false;
    try {
      app.activeDocument = doc;
      const top = doc.layers[0];
      const layer = await doc.createLayer({ name: job.layer_name || "ComfyUI" });
      if (top && top.id !== layer.id) await layer.move(top, constants.ElementPlacement.PLACEBEFORE);
      await writeResult(doc, layer, job, rgba, mask);
      const id = String(layer.id);
      committed = true;
      return id;
    } finally {
      try {
        if (previousLayers.length) doc.activeLayers = previousLayers;
      } catch (_) {
        // Layer selection is incidental to applying the pixel result.
      }
      try {
        await hostControl.resumeHistory(history, committed);
      } finally {
        try {
          if (previousDoc.id !== doc.id) app.activeDocument = previousDoc;
        } catch (_) {}
      }
    }
  }, { commandName: "ComfyUI · 回贴结果", timeOut: 1000 });
}
