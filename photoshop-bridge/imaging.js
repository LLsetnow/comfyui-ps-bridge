"use strict";

// UXP imaging flow adapted from rh-workflow-ps. Photoshop reads and writes
// stay inside modal scopes. PNG encoding and HTTP run outside those scopes.
const { app, core, imaging, action, constants } = require("photoshop");
const SRGB = "sRGB IEC61966-2.1";
const GRAY = "Gray Gamma 2.2";
const MAX_PIXELS = 32 * 1024 * 1024;

let lut16To8 = null;
let lut8To16 = null;
let lut8ToLinear = null;
let lut8ToAlpha = null;

function getLut16To8() {
  if (lut16To8) return lut16To8;
  lut16To8 = new Uint8Array(32769);
  for (let i = 0; i <= 32768; i++) {
    lut16To8[i] = Math.min(255, Math.round(i * 255 / 32768));
  }
  return lut16To8;
}

function getLut8To16() {
  if (lut8To16) return lut8To16;
  lut8To16 = new Uint16Array(256);
  for (let i = 0; i < 256; i++) {
    lut8To16[i] = Math.round(i * 32768 / 255);
  }
  return lut8To16;
}

function getLut8ToLinear() {
  if (lut8ToLinear) return lut8ToLinear;
  lut8ToLinear = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    lut8ToLinear[i] = srgbToLinear(i / 255);
  }
  return lut8ToLinear;
}

function getLut8ToAlpha() {
  if (lut8ToAlpha) return lut8ToAlpha;
  lut8ToAlpha = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    lut8ToAlpha[i] = i / 255;
  }
  return lut8ToAlpha;
}

function intersectRect(a, b) {
  if (!a || !b) return null;
  const left = Math.max(a.left, b.left);
  const top = Math.max(a.top, b.top);
  const right = Math.min(a.right, b.right);
  const bottom = Math.min(a.bottom, b.bottom);
  return right > left && bottom > top ? { left, top, right, bottom } : null;
}

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
  const out = new Uint8Array(data.length);
  if (depth === 16) {
    const lut = getLut16To8();
    for (let i = 0; i < data.length; i++) {
      const sample = data[i];
      out[i] = sample <= 0 ? 0 : sample >= 32768 ? 255 : lut[sample | 0];
    }
    return out;
  }
  const scale = 255;
  for (let i = 0; i < data.length; i++) {
    let sample = data[i];
    if (depth === 32 && color && i % components < 3) {
      sample = sample <= 0.0031308 ? sample * 12.92 : 1.055 * Math.pow(sample, 1 / 2.4) - 0.055;
    }
    const scaled = Math.round(sample * scale);
    out[i] = scaled <= 0 ? 0 : scaled >= 255 ? 255 : scaled;
  }
  return out;
}

function reframe(pixels, source, target, inChannels, outChannels) {
  if (
    inChannels === outChannels
    && source.left === target.left
    && source.top === target.top
    && source.right === target.right
    && source.bottom === target.bottom
  ) {
    return pixels;
  }
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
    if (outChannels === 3 && inChannels === 4) {
      for (let x = left; x < right; x++) {
        output[write] = pixels[read];
        output[write + 1] = pixels[read + 1];
        output[write + 2] = pixels[read + 2];
        read += 4;
        write += 3;
      }
      continue;
    }
    for (let x = left; x < right; x++) {
      const first = pixels[read];
      output[write] = first;
      if (outChannels >= 3) {
        output[write + 1] = inChannels >= 3 ? pixels[read + 1] : first;
        output[write + 2] = inChannels >= 3 ? pixels[read + 2] : first;
        if (outChannels === 4) {
          output[write + 3] = inChannels === 2 ? pixels[read + 1] : 255;
        }
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
    const pixels = normalize8(await image.getData({ chunky: true }), image.componentSize, image.components, channels >= 3);
    return reframe(pixels, source, rect, image.components, channels);
  } finally {
    image.dispose();
  }
}

async function capturePhotoshop(sessionId, options = {}) {
  const imageChannels = options.imageChannels === 4 ? 4 : 3;
  const selectionOnly = options.selectionOnly !== false;
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
    const depth = componentSize(doc);
    const layerOptions = { documentID: doc.id, layerID: layer.id };

    let layerPixels = null;
    let layerMask = null;
    let selectionPixels = null;
    let selectionMask = null;

    if (selectionRect && selectionOnly) {
      const overlap = intersectRect(layerRect, selectionRect);
      if (overlap) {
        selectionPixels = await readPixels(await imaging.getPixels({
          ...layerOptions,
          sourceBounds: overlap,
          colorSpace: "RGB",
          colorProfile: SRGB,
          componentSize: depth,
          applyAlpha: false
        }), selectionRect, imageChannels);
      } else {
        selectionPixels = new Uint8Array(rectWidth(selectionRect) * rectHeight(selectionRect) * imageChannels);
      }
      selectionMask = await readPixels(await imaging.getSelection({
        documentID: doc.id, sourceBounds: selectionRect
      }), selectionRect, 1);
    } else {
      layerPixels = await readPixels(await imaging.getPixels({
        ...layerOptions,
        sourceBounds: layerRect,
        colorSpace: "RGB",
        colorProfile: SRGB,
        componentSize: depth,
        applyAlpha: false
      }), layerRect, imageChannels);
      layerMask = properties.hasUserMask ? await readPixels(await imaging.getLayerMask({
        ...layerOptions, kind: "user", sourceBounds: layerRect
      }), layerRect, 1) : null;
      if (selectionRect) {
        selectionPixels = reframe(layerPixels, layerRect, selectionRect, imageChannels, imageChannels);
        selectionMask = await readPixels(await imaging.getSelection({
          documentID: doc.id, sourceBounds: selectionRect
        }), selectionRect, 1);
      }
    }

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
        layer_image: layerPixels ? { rect: layerRect, pixels: layerPixels, channels: imageChannels } : null,
        layer_mask: layerMask ? { rect: layerRect, pixels: layerMask, channels: 1 } : null,
        selection_image: selectionPixels ? { rect: selectionRect, pixels: selectionPixels, channels: imageChannels } : null,
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

function toNativeRgbaBuffer(pixels, pixelCount, depth) {
  const channels = pixels.length === pixelCount * 3 ? 3 : 4;
  if (depth === 8) {
    if (channels === 4) return pixels;
    const out = new Uint8Array(pixelCount * 4);
    for (let s = 0, d = 0; s < pixels.length; s += 3, d += 4) {
      out[d] = pixels[s];
      out[d + 1] = pixels[s + 1];
      out[d + 2] = pixels[s + 2];
      out[d + 3] = 255;
    }
    return out;
  }
  if (depth === 16) {
    const lut = getLut8To16();
    const out = new Uint16Array(pixelCount * 4);
    if (channels === 4) {
      for (let i = 0; i < out.length; i++) out[i] = lut[pixels[i]];
    } else {
      for (let s = 0, d = 0; s < pixels.length; s += 3, d += 4) {
        out[d] = lut[pixels[s]];
        out[d + 1] = lut[pixels[s + 1]];
        out[d + 2] = lut[pixels[s + 2]];
        out[d + 3] = 32768;
      }
    }
    return out;
  }
  const lutColor = getLut8ToLinear();
  const lutAlpha = getLut8ToAlpha();
  const out = new Float32Array(pixelCount * 4);
  if (channels === 4) {
    for (let i = 0; i < out.length; i += 4) {
      out[i] = lutColor[pixels[i]];
      out[i + 1] = lutColor[pixels[i + 1]];
      out[i + 2] = lutColor[pixels[i + 2]];
      out[i + 3] = lutAlpha[pixels[i + 3]];
    }
  } else {
    for (let s = 0, d = 0; s < pixels.length; s += 3, d += 4) {
      out[d] = lutColor[pixels[s]];
      out[d + 1] = lutColor[pixels[s + 1]];
      out[d + 2] = lutColor[pixels[s + 2]];
      out[d + 3] = 1;
    }
  }
  return out;
}

async function writeResult(doc, layer, job, pixels, mask) {
  const depth = componentSize(doc);
  // Photoshop expects native-depth RGBA to preserve alpha when writing pixels.
  const buffer = toNativeRgbaBuffer(pixels, job.width * job.height, depth);

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
