# ComfyUI PS Bridge

在 Photoshop 捕获活动图层和画布选区，在 ComfyUI 编辑工作流并运行。工作流成功完成后，Photoshop 把结果放回捕获时的文档与坐标，以带可编辑图层蒙版的新图层呈现。

| 目录 | 用途 |
| --- | --- |
| `photoshop-bridge/` | Adobe Photoshop UXP 面板，负责捕获、连接和回贴 |
| `comfyui-nodes/` | ComfyUI 自定义节点及桥接接口，随 ComfyUI 进程运行 |

## 安装

1. 将 `comfyui-nodes/` 整个目录复制到运行 ComfyUI 的机器上，放在 `ComfyUI/custom_nodes/comfyui-ps-bridge/`。确认目标目录直接包含 `__init__.py`。重启 ComfyUI 并刷新浏览器，在节点菜单的 `Photoshop Bridge` 分类中找到三个节点，也可搜索 `PS Bridge`。 [ComfyUI 安装自定义节点](https://docs.comfy.org/installation/install_custom_node)
2. 启动 Photoshop 25.0 或更新版本，打开 Adobe UXP Developer Tools。首次使用时启用 Developer Mode，点 **Add Plugin** 并选择 `photoshop-bridge/manifest.json`，再点 **Load**。面板关闭后，从 Photoshop 的「增效工具 → ComfyUI PS Bridge → ComfyUI 桥接面板」打开。重启 Photoshop 后，如果菜单里没有这项，可回到 UXP Developer Tools 点 **Load**。 [Adobe 开发者工具设置](https://developer.adobe.com/uxp/guides/how-to/developer-tools/) · [添加插件](https://developer.adobe.com/uxp/guides/how-to/udt-deep-dive/plugin-management) · [加载插件](https://developer.adobe.com/uxp/guides/how-to/udt-deep-dive/plugin-workflows)
3. 在面板填入 ComfyUI 的 `IP:端口`（如本机填 `127.0.0.1:8188`，SSH 转发填 `127.0.0.1:8189`，局域网直连填 `192.168.x.x:8188`），点「连接」即可，无需令牌。

拖动浮动桥接面板的边缘或右下角可以调整宽高。停靠时拖动面板区的分隔线。缩矮后可在面板内纵向滚动。

Photoshop 桥接接口允许本机回环、局域网/内网（含 Tailscale 等 `100.64.0.0/10` 网段）及同机 SSH 转发请求。

## 捕获与选节点

在 Photoshop 选中要处理的图层。需要对局部操作时，在画布圈出选区。保持桥接面板可见且已连接，在 ComfyUI 的任一 Load 节点点「捕获并预览」。两个 Load 节点会共用这次快照并显示各自的输入。也可以从 Photoshop 面板点「捕获当前图层」。每次运行使用入队时的最新快照。之后切换图层或再次捕获，已入队任务仍使用原来的像素、蒙版和坐标。编辑原图、选区或图层蒙版后，重新捕获即可更新下一次运行的输入。

| 节点 | 设置 | 输出或效果 |
| --- | --- | --- |
| `PS Bridge · Layer Name` | 在多行文本框中输入图层名称 | 输出字符串，可连接到 `PS Bridge · Save Image` 的 `layer_name` 输入 |
| `PS Bridge · Load Image` | 自动根据选区判断 | 有选区时读取选区外接矩形内的图像，否则读取整层图像。输出图像、`frame` 和同尺寸全白 `mask`，在节点里显示图像预览 |
| `PS Bridge · Load Mask` | 自动根据选区判断 | 有选区时读取选区蒙版并保留羽化，否则读取图层的像素蒙版。在节点里显示灰度预览 |
| `PS Bridge · Save Image` | `image`、原始 `frame`、`layer_name`，可选 `mask` | 显示应用回贴蒙版后的图像预览。工作流成功后由 Photoshop 面板创建新图层 |

将加载图像节点的 `frame` 同时接到加载蒙版和保存节点。这样蒙版会按图像所在的文档坐标裁取或补边，保存节点可恢复原始边界。没有选区时，`Load Mask` 需要活动图层具备像素蒙版。蒙版的 1 为白色，回贴后显示图层像素。0 为黑色，回贴后隐藏像素。

保存节点会把图像和蒙版调整到 `frame` 的像素尺寸，并按原始左上角放回。显式连接 `mask` 可以回贴工作流处理后的蒙版。如果留空，选区图像沿用捕获时的选区蒙版，整层图像沿用捕获时的图层蒙版，缺少图层蒙版时使用全白蒙版。每个保存节点接收一张图像和一张蒙版。需要回贴多层时，为每层接一个保存节点。

加载图像节点输出 ComfyUI 标准的 RGB 图像。回贴图层的可见范围由蒙版控制，保存节点也会保留工作流输出的 RGBA 图像自身的透明度。

回贴使用捕获时的 Photoshop 会话和文档。运行期间保持该文档打开、画布尺寸不变，并保持 Photoshop 面板可见以便自动接收结果。ComfyUI 将快照和待回贴结果存放在进程内存中。快照自捕获或最近一次排队起保留 24 小时，待回贴结果最多保留 2 小时，重启后需重新捕获。单个矩形最多约 3355 万像素，单次捕获上传的 JSON 最大 96 MiB。

## 不用模型的验收

1. 在测试文档中创建一块不透明的纯色像素，使图层的左上角离开画布原点。在色块内画一个有羽化的选区，再捕获。
2. 把[往返测试工作流](comfyui-nodes/workflows/round-trip.json)拖入 ComfyUI，或按下图连接节点。在任一 Load 节点点「捕获并预览」，确认两个预览分别是选区外接矩形内的图像和灰度蒙版。运行后，Save 节点显示蒙版合成预览，Photoshop 在原选区位置创建带可编辑蒙版的新图层。

   ```text
   Load Image.image ───────────────────────────▶ Save Image.image
   Load Image.frame ───────────────────────────▶ Save Image.frame
   Load Image.frame ──▶ Load Mask.frame
   Load Mask.mask ─────────────────────────────▶ Save Image.mask
   ```

3. 在原图层上创建像素蒙版并取消画布选区。重新点「捕获并预览」，确认节点切换到整层图像与图层蒙版。再次运行并核对新图层位置和蒙版像素。

2026-11-20 实测使用 macOS 上的 Photoshop 27.11.0，面板内容区为 240 × 278 px。通过 AiServer 的 SSH 转发连接 `4070win` 上的 ComfyUI 0.46.0。整层和选区两条分支在同一次运行中自动回贴，Photoshop 像素回读确认了两个矩形的位置、图像颜色、选区羽化灰度、图层蒙版灰度及矩形外隐藏。验收只使用合成文档和无模型工作流，合成文档已关闭且未保存。
