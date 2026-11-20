import { app } from '../../scripts/app.js'
import { api } from '../../scripts/api.js'

const bridge = '/ps-bridge/v1'
const nodeKinds = {
  PSBridgeLoadImage: 'image',
  PSBridgeLoadMask: 'mask'
}
const captureLabel = '捕获并预览'
let captureInFlight = null

async function bridgeRequest(path, options) {
  const response = await api.fetchApi(bridge + path, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.error || `Photoshop 桥接请求失败（HTTP ${response.status}）`)
  }
  return data
}

async function requestCapture() {
  const { request_id: id } = await bridgeRequest('/capture-requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  })
  const deadline = Date.now() + 137000
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 650))
    const state = await bridgeRequest('/capture-requests/' + encodeURIComponent(id))
    if (state.error) throw new Error(state.error)
    if (state.capture_id) return state.capture_id
  }
  throw new Error('等待 Photoshop 捕获超时，请确认桥接面板可见。')
}

function hideCaptureIdWidget(node) {
  const widget = node.widgets?.find(item => item.name === 'capture_id')
  if (!widget) return null
  widget.type = 'hidden'
  widget.hidden = true
  widget.computeSize = () => [0, -4]
  widget.draw = () => {}
  return widget
}

function setNodeCaptureId(node, captureId) {
  const widget = node.widgets?.find(item => item.name === 'capture_id')
  if (widget) widget.value = captureId || ''
}

function displayPreview(node, image) {
  const key = node.graph === app.rootGraph ? String(node.id) : `${node.graph.id}:${node.id}`
  if (image) {
    // Setting the native output also feeds the Vue node renderer.
    app.nodeOutputs[key] = { images: [image] }
  } else {
    delete app.nodeOutputs[key]
    node.imgs = undefined
    node.images = undefined
    const previewWidget = node.widgets?.find(widget => widget.name === '$$canvas-image-preview')
    if (previewWidget) node.removeWidget(previewWidget)
  }
  node.setDirtyCanvas(true, true)
}

async function refreshPreview(origin, root, captureId) {
  if (!origin.graph || app.rootGraph !== root) return
  const kind = nodeKinds[origin.comfyClass]
  const query = new URLSearchParams({ capture_id: captureId, kind })
  try {
    const { image } = await bridgeRequest(`/capture-preview?${query}`)
    if (!origin.graph || app.rootGraph !== root) return
    setNodeCaptureId(origin, captureId)
    displayPreview(origin, image)
  } catch (error) {
    if (!origin.graph || app.rootGraph !== root) return
    displayPreview(origin, null)
    app.extensionManager.toast.warning('预览不可用', { description: error.message })
  }
}

async function captureAndPreview(node) {
  if (captureInFlight) return
  const root = app.rootGraph
  const toast = app.extensionManager.toast
  const progress = toast.loading('正在从 Photoshop 捕获…')
  captureInFlight = requestCapture()
  try {
    await refreshPreview(node, root, await captureInFlight)
  } catch (error) {
    toast.error('Photoshop 捕获失败', { description: error.message })
  } finally {
    toast.dismiss(progress)
    captureInFlight = null
  }
}

app.registerExtension({
  name: 'PhotoshopBridge.CapturePreview',
  nodeCreated(node) {
    if (!Object.hasOwn(nodeKinds, node.comfyClass)) return
    hideCaptureIdWidget(node)
    const widget = node.addWidget('button', captureLabel, null, () => { void captureAndPreview(node) }, { serialize: false })
    widget.serialize = false
  }
})
