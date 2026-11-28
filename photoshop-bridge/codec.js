"use strict";

// Binary framing and fast lossless codecs for Photoshop <-> ComfyUI transfers.
// PSBR: [magic "PSBR"][version=1 u32le][metaLen u32le][meta UTF-8 JSON][asset payloads...]
// PSBD: [magic "PSBD"][version=1 u32le][channels u32le][imgCodec u32le][maskCodec u32le][imgLen u32le][maskLen u32le][img][mask]
const PSBR_HEADER_BYTES = 12;
const PSBD_HEADER_BYTES = 28;
const BUNDLE_CODEC_RAW = 0;
const BUNDLE_CODEC_RLE = 1;

let deflateFormatPromise = null;

function encodeUtf8(text) {
  if (typeof TextEncoder === "function") {
    return new TextEncoder().encode(text);
  }
  const encoded = unescape(encodeURIComponent(text));
  const bytes = new Uint8Array(encoded.length);
  for (let i = 0; i < encoded.length; i++) {
    bytes[i] = encoded.charCodeAt(i);
  }
  return bytes;
}

function writeU32LE(view, offset, value) {
  view[offset] = value & 0xff;
  view[offset + 1] = (value >>> 8) & 0xff;
  view[offset + 2] = (value >>> 16) & 0xff;
  view[offset + 3] = (value >>> 24) & 0xff;
}

function readU32LE(view, offset) {
  return (
    view[offset]
    | (view[offset + 1] << 8)
    | (view[offset + 2] << 16)
    | (view[offset + 3] << 24)
  ) >>> 0;
}

function rleEncode(data) {
  const len = data.length;
  const out = new Uint8Array(len + Math.ceil(len / 128) + 16);
  let i = 0;
  let op = 0;
  while (i < len) {
    const value = data[i];
    if (i + 2 < len && data[i + 1] === value && data[i + 2] === value) {
      let run = 3;
      const maxRun = Math.min(130, len - i);
      while (run < maxRun && data[i + run] === value) run++;
      out[op++] = 0x80 | (run - 3);
      out[op++] = value;
      i += run;
      continue;
    }
    const litStart = i;
    i++;
    const maxLit = Math.min(litStart + 128, len);
    while (i < maxLit) {
      if (i + 2 < len && data[i] === data[i + 1] && data[i] === data[i + 2]) break;
      i++;
    }
    const litLen = i - litStart;
    out[op++] = litLen - 1;
    out.set(data.subarray(litStart, i), op);
    op += litLen;
  }
  return out.subarray(0, op);
}

function rleDecode(src, expectedLength) {
  const out = new Uint8Array(expectedLength);
  let i = 0;
  let op = 0;
  const len = src.length;
  while (i < len) {
    const control = src[i++];
    if (control & 0x80) {
      if (i >= len) throw new Error("蒙版压缩数据截断");
      const count = (control & 0x7f) + 3;
      if (op + count > expectedLength) throw new Error("蒙版压缩数据长度越界");
      out.fill(src[i++], op, op + count);
      op += count;
    } else {
      const count = control + 1;
      if (i + count > len || op + count > expectedLength) {
        throw new Error("蒙版压缩数据损坏");
      }
      out.set(src.subarray(i, i + count), op);
      i += count;
      op += count;
    }
  }
  if (op !== expectedLength) throw new Error("蒙版解压长度与预期不符");
  return out;
}

function subFilterPixels(pixels, width, height, channels) {
  const stride = width * channels;
  const out = new Uint8Array(stride * height);
  for (let y = 0; y < height; y++) {
    const row = y * stride;
    for (let x = 0; x < channels; x++) {
      out[row + x] = pixels[row + x];
    }
    for (let x = channels; x < stride; x++) {
      out[row + x] = (pixels[row + x] - pixels[row + x - channels]) & 0xff;
    }
  }
  return out;
}

async function detectDeflateFormat() {
  if (typeof CompressionStream !== "function") return null;
  for (const format of ["deflate", "gzip"]) {
    try {
      const stream = new CompressionStream(format);
      const writer = stream.writable.getWriter();
      const reader = stream.readable.getReader();
      await writer.write(new Uint8Array([1, 2, 3, 4]));
      await writer.close();
      while (!(await reader.read()).done) {}
      return format;
    } catch (_) {
      // Try next supported format.
    }
  }
  return null;
}

async function compressWithNativeStream(bytes) {
  if (!deflateFormatPromise) deflateFormatPromise = detectDeflateFormat();
  const format = await deflateFormatPromise;
  if (!format) return null;
  try {
    const stream = new CompressionStream(format);
    const writer = stream.writable.getWriter();
    const writePromise = writer.write(bytes).then(() => writer.close());
    const reader = stream.readable.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
    }
    await writePromise;
    const out = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  } catch (_) {
    deflateFormatPromise = Promise.resolve(null);
    return null;
  }
}

async function encodeBinaryAsset(asset) {
  const width = asset.rect.right - asset.rect.left;
  const height = asset.rect.bottom - asset.rect.top;
  const pixels = asset.pixels;
  const channels = asset.channels;

  if (channels === 1) {
    const rle = rleEncode(pixels);
    if (rle.length < pixels.length * 0.9) {
      return { rect: asset.rect, channels, codec: "rle", bytes: rle };
    }
    const deflated = await compressWithNativeStream(pixels);
    if (deflated && deflated.length < pixels.length * 0.95) {
      return { rect: asset.rect, channels, codec: "deflate", bytes: deflated };
    }
    return { rect: asset.rect, channels, codec: "raw", bytes: pixels };
  }

  const sub = subFilterPixels(pixels, width, height, channels);
  const deflated = await compressWithNativeStream(sub);
  if (deflated && deflated.length < pixels.length * 0.95) {
    return { rect: asset.rect, channels, codec: "sub+deflate", bytes: deflated };
  }
  const rle = rleEncode(sub);
  if (rle.length < pixels.length * 0.88) {
    return { rect: asset.rect, channels, codec: "sub+rle", bytes: rle };
  }
  return { rect: asset.rect, channels, codec: "raw", bytes: pixels };
}

function buildCapturePacket(snapshot, encodedAssets) {
  const assetDescriptors = {};
  const chunks = [];
  let payloadBytes = 0;
  for (const [name, item] of Object.entries(encodedAssets)) {
    if (!item) {
      assetDescriptors[name] = null;
      continue;
    }
    assetDescriptors[name] = {
      rect: item.rect,
      channels: item.channels,
      codec: item.codec,
      offset: payloadBytes,
      byte_length: item.bytes.length
    };
    chunks.push(item.bytes);
    payloadBytes += item.bytes.length;
  }
  const metadata = {
    session_id: snapshot.session_id,
    document_id: snapshot.document_id,
    document_name: snapshot.document_name,
    document_width: snapshot.document_width,
    document_height: snapshot.document_height,
    layer_id: snapshot.layer_id,
    layer_name: snapshot.layer_name,
    layer_rect: snapshot.layer_rect,
    selection_rect: snapshot.selection_rect,
    assets: assetDescriptors
  };
  if (snapshot.request_id) metadata.request_id = snapshot.request_id;
  const metaBytes = encodeUtf8(JSON.stringify(metadata));
  const packet = new Uint8Array(PSBR_HEADER_BYTES + metaBytes.length + payloadBytes);
  packet[0] = 0x50; // P
  packet[1] = 0x53; // S
  packet[2] = 0x42; // B
  packet[3] = 0x52; // R
  writeU32LE(packet, 4, 1);
  writeU32LE(packet, 8, metaBytes.length);
  packet.set(metaBytes, PSBR_HEADER_BYTES);
  let cursor = PSBR_HEADER_BYTES + metaBytes.length;
  for (const chunk of chunks) {
    packet.set(chunk, cursor);
    cursor += chunk.length;
  }
  return packet;
}

function parseDeliveryBundle(packet, width, height) {
  if (!(packet instanceof Uint8Array) || packet.length < PSBD_HEADER_BYTES) {
    throw new Error("回贴数据包长度无效");
  }
  if (packet[0] !== 0x50 || packet[1] !== 0x53 || packet[2] !== 0x42 || packet[3] !== 0x44) {
    throw new Error("回贴数据包头标识无效");
  }
  const version = readU32LE(packet, 4);
  if (version !== 1) throw new Error("不支持的回贴数据包版本：" + version);
  const channels = readU32LE(packet, 8);
  if (channels !== 3 && channels !== 4) throw new Error("回贴图像通道数无效");
  const imageCodec = readU32LE(packet, 12);
  const maskCodec = readU32LE(packet, 16);
  const imageLength = readU32LE(packet, 20);
  const maskLength = readU32LE(packet, 24);
  if (PSBD_HEADER_BYTES + imageLength + maskLength !== packet.length) {
    throw new Error("回贴数据包内容长度不匹配");
  }
  const pixelCount = width * height;
  const rawImageSlice = packet.subarray(PSBD_HEADER_BYTES, PSBD_HEADER_BYTES + imageLength);
  const rawMaskSlice = packet.subarray(PSBD_HEADER_BYTES + imageLength);

  let pixels;
  if (imageCodec === BUNDLE_CODEC_RAW) {
    if (rawImageSlice.length !== pixelCount * channels) throw new Error("回贴图像字节长度不符");
    pixels = packet.slice(PSBD_HEADER_BYTES, PSBD_HEADER_BYTES + imageLength);
  } else if (imageCodec === BUNDLE_CODEC_RLE) {
    pixels = rleDecode(rawImageSlice, pixelCount * channels);
  } else {
    throw new Error("不支持的回贴图像编码：" + imageCodec);
  }

  let mask;
  if (maskCodec === BUNDLE_CODEC_RAW) {
    if (rawMaskSlice.length !== pixelCount) throw new Error("回贴蒙版字节长度不符");
    mask = packet.slice(PSBD_HEADER_BYTES + imageLength);
  } else if (maskCodec === BUNDLE_CODEC_RLE) {
    mask = rleDecode(rawMaskSlice, pixelCount);
  } else {
    throw new Error("不支持的回贴蒙版编码：" + maskCodec);
  }

  return { pixels, mask, channels };
}
