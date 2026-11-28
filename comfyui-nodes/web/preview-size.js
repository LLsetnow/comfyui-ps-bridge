const previewSubfolder = '__photoshop_bridge_previews__'
const canvasPreview = '$$canvas-image-preview'
const wrappedWidgets = new WeakSet()

function sourceSize(image) {
  const src = image?.currentSrc || image?.src
  if (!src || !image.naturalWidth || !image.naturalHeight) return null
  const url = new URL(src, document.baseURI)
  if (url.origin !== window.location.origin || url.searchParams.get('subfolder') !== previewSubfolder) return null
  const filename = url.searchParams.get('filename')
  const match = /^[\da-f-]{36}_([1-9]\d*)x([1-9]\d*)\.png$/.exec(filename)
  return match ? `${match[1]} × ${match[2]}` : null
}

export function showSourcePreviewSize(node) {
  const onDrawForeground = node.onDrawForeground
  node.onDrawForeground = function (...args) {
    onDrawForeground?.apply(this, args)
    const widget = this.widgets?.find(item => item.name === canvasPreview)
    if (!widget?.drawWidget || wrappedWidgets.has(widget)) return
    wrappedWidgets.add(widget)

    // ComfyUI draws the caption inside its image widget. Replace just the
    // caption during that draw so the thumbnail layout and controls stay native.
    const drawWidget = widget.drawWidget
    widget.drawWidget = function (ctx, options) {
      const images = options?.previewImages ?? node.imgs
      const image = images?.[node.imageIndex ?? 0]
      const caption = sourceSize(image)
      if (!caption) return drawWidget.call(this, ctx, options)
      const thumbnailSize = `${image.naturalWidth} × ${image.naturalHeight}`
      const fillText = ctx.fillText
      const ownFillText = Object.getOwnPropertyDescriptor(ctx, 'fillText')
      ctx.fillText = function (text, ...position) {
        return fillText.call(this, text === thumbnailSize ? caption : text, ...position)
      }
      try {
        return drawWidget.call(this, ctx, options)
      } finally {
        if (ownFillText) Object.defineProperty(ctx, 'fillText', ownFillText)
        else delete ctx.fillText
      }
    }
  }
}

export function watchSourcePreviewSizes() {
  // Vue also derives its native caption from the decoded thumbnail. Wait for
  // its load handler to render that caption before updating the text.
  document.addEventListener('load', event => {
    const image = event.target
    if (!(image instanceof HTMLImageElement) || !image.matches('[data-testid="main-image"]')) return
    const caption = sourceSize(image)
    const gallery = image.closest('.image-preview > [role="region"]')
    if (!caption || !gallery) return
    const url = image.currentSrc || image.src
    requestAnimationFrame(() => {
      if (!image.isConnected || (image.currentSrc || image.src) !== url) return
      const label = gallery.nextElementSibling?.querySelector(':scope > span:only-child')
      const thumbnailSize = `${image.naturalWidth} x ${image.naturalHeight}`
      if (label?.textContent.trim() === thumbnailSize) label.textContent = caption
    })
  }, true)
}
