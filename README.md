# Video Mover: 多平台内容分发自动化流水线

[简体中文](./README.md) | [English](./README_en.md)

面向多平台内容运营场景的视频处理与发布工作流。

该项目将文件监听、视频标准化处理、文案生成、任务调度和多平台发布整合到一条可配置流水线中，用于探索如何减少内容团队在重复操作上的时间投入。流水线各阶段均为独立模块，可按业务需要组合、替换或关闭。

> 本项目仅应用于拥有合法使用权的内容与账号。使用者应遵守内容版权、平台规则和当地法律法规。

## 项目背景

跨平台内容运营通常包含多个相互割裂的步骤：素材进入、格式处理、文案整理、发布时间安排和逐平台上传。重复的人工操作不仅耗时，也容易造成命名、参数和发布记录不一致。

Video Mover 将这些步骤抽象为可组合的任务模块，以便对流程进行自动化、监控和扩展。项目的目标不是替代创作环节，而是把"处理—文案—调度—发布"这类确定性的重复劳动交给流水线，让人保留在素材选择与内容终审环节。

## 特别鸣谢

<p align="center">
  <a href="https://www.ipwo.net/?ref=githubvideomover" target="_blank">
    <img src="images/IPWO.png" alt="IPWO" width="800">
  </a>
</p>

<p align="center">
  <b><a href="https://www.ipwo.net/?ref=githubvideomover" target="_blank">IPWO</a></b>
  住宅代理提供 195+ 地区动静态住宅代理，适用于 TikTok、社交媒体运营、多平台内容分发、浏览器自动化及数据获取等场景，为开发者和运营团队提供不同地区的网络环境选择。
  <br>
  包含 HTTP(S) / SOCKS5 多协议，充裕住宅流量供免费测试，优惠码：<code>0205</code>，
  <a href="https://www.ipwo.net/?ref=githubvideomover">访问入口</a>
</p>

## 主要能力

- **目录监听与任务触发**：基于 Watchdog 持续监听指定素材目录，新文件进入后自动触发处理任务，支持 7×24 小时无人值守运行。
- **内容差异化处理工具箱**：基于 FFmpeg / OpenCV 的可配置处理链，覆盖自动字幕、自定义标题、背景音乐（BGM）、画中画（PIP）、静音剪辑、镜像、旋转、裁剪、淡入淡出、画质调整，以及背景模糊、帧交换、颜色偏移、纹理噪声等数十种视觉处理项。所有功能独立可配、自由组合，服务于二次创作与素材规范化场景。
- **GPU 加速**：在兼容环境下调用 NVIDIA 显卡进行硬件编码加速，显著缩短批量处理时间。
- **AI 文案草稿生成**：接入阿里云百炼大模型，分析视频内容后生成标题与标签草稿，发布前由人工终审确认后使用。
- **任务队列与定时调度**：通过 APScheduler 配置任务队列与发布时间表，统一维护执行状态。
- **多平台发布适配**：基于 Playwright 的浏览器自动化，适配 TikTok、抖音、快手、哔哩哔哩、视频号和小红书等平台的上传流程，自动完成登录态复用与表单填写。
- **可扩展架构**：通过插件与 Handler 结构扩展处理步骤及上传适配器，新增平台或处理项无需改动主流程。
- **日志与状态记录**：完整记录任务日志与执行状态，便于失败定位与流程复盘。

## 📸 软件截图

<p align="center">
  <a href="https://www.bilibili.com/video/BV1txQeYyEEz" target="_blank">
    <img src="./images/cover_demo.png" alt="软件界面演示" width="800"/>
  </a>
  <br>
  <em>软件界面（点击封面跳转 B 站观看演示视频）</em>
</p>

<p align="center">
  <img src="./images/cover_script.png" alt="脚本运行展示" width="800"/>
  <br>
  <em>脚本运行过程展示</em>
</p>

## 工作流

```text
Content input
    -> File watcher
    -> Video processing
    -> Metadata generation
    -> Scheduling
    -> Platform adapters
    -> Execution logs
```

## 技术设计

- `main.py`：默认的抖音单视频下载入口，不执行加工和上传
- `pipeline.py`：旧版 TikTok 任务编排、文件管理、调度与插件入口
- `Dedup/`：基于 FFmpeg、OpenCV 的视频处理模块（对应上方工具箱的各项处理）
- `Upload/`：各平台上传适配器与账号配置
- `my_apps.yaml`：应用与网络参数配置（输入目录、目标平台、代理、调度等）

主要技术：Python、FFmpeg、OpenCV、Playwright、Watchdog、APScheduler、LLM API。

## 快速开始

### 环境要求

- Windows
- Python 3.12+
- Node.js 22.x
- FFmpeg（需加入 `PATH`）
- Chrome

### 安装

1. 克隆仓库：

   ```bash
   git clone https://github.com/toki-plus/video-mover.git
   cd video-mover
   ```

2. 运行 `setup.bat`，自动安装 Python、Node.js、Playwright 与相关依赖。建议在独立虚拟环境中运行。

3. **依赖补丁（关键步骤）**：为兼容目标平台接口，需对虚拟环境中的两处第三方库做小改动：
   - `f2/apps/tiktok/handler.py`（约 389 行）：`params={"cursor": cursor, ...}` 改为 `params={"cursor": int(cursor), ...}`；
   - `f2/utils/utils.py`（约 200 / 690 行）：扩展日期字符串解析逻辑，同时兼容 `YYYY-MM-DD` 与带时间的格式；
   - `tencent_uploader/main.py`（约 191 行）：将页面跳转等待超时由 1500ms 提升至 10000ms，避免慢网络下误判失败。

### 配置

1. 在 `my_apps.yaml` 中配置输入目录、目标平台、账号文件、代理和调度参数。
2. 如启用 AI 文案功能，通过安全的本地配置方式提供 API Key，切勿提交到仓库。
3. 首次运行平台适配器时，根据提示完成账号登录与 Cookie 初始化。

### 运行

第一步仅下载抖音视频，无需安装视频加工和上传依赖。PowerShell 中执行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-download.txt
$env:PYTHONUTF8 = '1'
& .\.venv\Scripts\python.exe .\main.py
```

按提示粘贴有权下载的单条视频链接或分享文案，也可直接传入：

```powershell
& .\.venv\Scripts\python.exe .\main.py "https://www.douyin.com/video/视频ID"
```

默认以项目的 `Download\douyin` 为根目录，每个视频独立保存到 `<视频ID>` 子目录；
例如 `Download\douyin\7679686176123636901\` 中包含该视频的 MP4、封面、音乐及所有资料文件。
`--output` 指定的是根目录，程序仍会在其中建立视频 ID 子目录。
双击 `start.bat` 也会启动此下载入口，并优先使用项目虚拟环境，不再删除上传素材。
默认使用不含凭据的 `douyin.example.yaml`，不会读取旧版 `my_apps.yaml`。
如需登录态，将示例复制为已被 Git 忽略的 `douyin.local.yaml`，在本机填写抖音网页 Cookie：

```powershell
Copy-Item .\douyin.example.yaml .\douyin.local.yaml
& .\.venv\Scripts\python.exe .\main.py --config .\douyin.local.yaml
```

不要提交或分享 Cookie。默认入口不会自动打开浏览器登录，也不会绕过访问限制。
本地配置支持可选的 `douyin.uifid` 字符串，适配入口会将其作为 `uifid` 请求头发送；
`douyin.headers` 中的自定义请求头也会保留，不再被 F2 CLI 默认值覆盖。
同名请求头不区分大小写，独立的 `douyin.uifid` 优先于 `headers.uifid`。
UIFID 同样属于敏感信息，仅填写浏览器正常请求中使用的真实值，不要提交或分享。
传入此字段不保证解决平台返回的 403。

#### 浏览器登录下载（推荐用于网页签名不兼容的情况）

安装更新后的下载依赖，确保本机已安装 Microsoft Edge，然后运行：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-download.txt
& .\.venv\Scripts\python.exe .\main.py --browser-login
```

程序先打开独立 Edge 窗口，请手动登录抖音。登录后浏览器保持打开，
在终端连续粘贴视频或图文链接，程序会串行读取匹配作品 ID 的正常网页接口响应。
视频交给 F2 下载；图文按原顺序保存接口提供的图片，不转换为 MP4。
输入链接前请点击终端使其获得焦点，使用终端的粘贴操作后按回车；
交互输入使用异步行编辑器，支持编辑、粘贴和历史记录，不阻塞浏览器事件循环。
实时下载进度只在下载期间显示，不与等待链接的输入编辑器同时刷新终端。
每条任务完成后自动等待下一条，不需要再次启动浏览器或手动复制 Cookie。
也可以在命令中提供第一条链接，处理完成后仍进入连续输入模式。

输入 `exit`、`quit` 或按 `Ctrl+C` 可结束会话并关闭浏览器；终端输入结束或手动关闭浏览器也会结束会话。
单条链接错误或下载失败会明确报错，但保留浏览器并允许继续下一条。

#### 单条视频与图文资料归档

浏览器模式和直接接口模式使用同一套归档逻辑（直接接口模式仍可能因平台签名返回 403）。
每条视频的文件都位于其 ID 子目录内。重新处理同一视频时复用该目录，
不会为每次运行建立重复目录。旧版根目录下以该视频 ID 加下划线开头的文件会自动移入；
若目标存在同名文件，会报错并在移动前停止，不会覆盖，请先手动解决冲突。
除 `<视频ID>_video.mp4` 外，会保存：

- 图文作品（`aweme_type=68`）保存为 `<作品ID>_images_0.*`、`_images_1.*` 等，
  从 0 开始编号，顺序与原始 `images` 数组一致；不要求存在 MP4。
  元数据和清单记录 `media_type=image_gallery`、图片数量，元数据另存原始图片字段。
  任意图片缺失、下载失败或校验失败均不能判为图片下载成功；保留已有资料，重新输入链接可重试。
  仅保存图片，不下载 Live Photo 的动态片段，也不自动生成图文视频。
- `<视频ID>_detail_raw.json`：匹配该视频的完整原始作品对象，包括接口提供但尚未专门解析的字段；
  不是整个网络响应包，不包含程序额外读取的 Cookie 或请求头。
- `<视频ID>_metadata.json`：描述、作者、发布时间（原时间戳及 UTC）、标签/话题、统计、
  音乐和视频信息；缺失字段明确列出，不编造值。统计仅代表本次抓取时刻。
- `<视频ID>_desc.txt`：原始描述。
- `<视频ID>_video_cover.*`、`_video_origin_cover.*`、`_video_dynamic_cover.*`、
  `_video_animated_cover.*`：接口实际提供的各版本封面，分别保存，而不是截取第一帧。
  缺失版本不会生成文件；扩展名按文件头识别，支持 JPEG、PNG、WebP、GIF、AVIF。
- `<视频ID>_music_play_url.*`：接口提供且允许获取的音乐音频，支持 MP3、M4A、AAC、Ogg、WAV。
  这是平台音乐素材，不保证与视频内最终混音完全相同，也不会从 MP4 提取音轨。
- `<视频ID>_archive.json`：归档范围、文件大小、SHA-256 和每项素材状态
  （下载完成、复用、未提供、平台不可用或失败）。`partial` 表示视频/图文图片缺失或存在素材下载失败；
  `captured_response_archived` 只表示本次响应及可获取素材已归档，不代表拿到了平台所有信息。

素材地址有多个候选时会依次尝试；空内容、HTML/JSON 错误页、无法识别的媒体会记为失败，
不会假装下载成功。重新输入链接会重试失败项；已有素材需与旧清单的大小和 SHA-256 一致才复用。
该检查不代替完整媒体解码验证。不会绕过登录、版权或平台下载限制。
完整作品 JSON 可能含作者标识、临时媒体链接和跟踪参数；仅在本机保存，不要直接公开分享。
本次范围不包含评论、作者其他作品，也不保证单个接口提供页面上的所有信息。

此外会保存以下三份商品文件：

- `<视频ID>_products.txt`：商品名称、ID、真实网页/App 链接、字段来源和覆盖范围说明。
- `<视频ID>_products.json`：结构化提取结果（schema version 2）、候选/识别/未识别记录数及原始证据文件的 SHA-256。
- `<视频ID>_products_raw.json`：本次响应中 `anchor_info`、`anchors`、`promotions`、
  `commerce_config_data` 的原始值、字段缺失/空值状态，以及 `anchor_info.extra` 等字段的 JSON 解码结果。

原始 `extra` 字符串位于证据文件的 `raw_fields.anchor_info.extra`；
可读解码列表位于 `decoded_fields` 中 `anchor_info.extra` 的 `value`。
常驻浏览器会话还会记录对应接口的域名、路径和 HTTP 状态，不保存带签名的请求查询串。
商品原始证据只覆盖列出的商品字段；完整作品对象另存于 `_detail_raw.json`，
不保存 Cookie、请求头或整个网络响应包。
证据可能含有平台临时链接和跟踪参数，建议仅在本机保存，不要直接公开分享。
程序不根据 ID 拼造商品链接。

`found` 只表示本次识别到的商品都有网页链接，**不表示全部关联商品均已获取**；
`partial` 表示仅有部分信息或解析警告，
`not_found` 表示本次响应未提供可识别的商品信息，不代表该视频没有商品。
`coverage.all_video_products_verified` 当前始终为 `false`，因为尚未自动与页面商品列表
或独立商品接口核对。TXT 和终端也会明确提示这一限制，不能据此声称商品全量完整。
商品 App 链接会单独标注，不能当作可直接打开的网页链接。

已存在的目标非空 MP4 会提示“已存在”并跳过视频下载，但仍重新读取作品数据，
更新资料/商品文件并补充缺失的封面和音乐；重新输入链接即可补资料，不需要删除 MP4。
此判断按视频 ID 和文件名进行，不会重新解码检查已有文件。
登录失效时可在保留的窗口中重新登录；如出现平台验证，请手动完成，必要时刷新视频页面。
等待登录或单条视频数据最多 5 分钟；空闲等待输入没有此超时。

登录态仅保存在项目的 `.browser-profile\douyin` 独立浏览器目录，已被 Git 忽略；
不读取日常浏览器配置、不改写 `douyin.local.yaml`，也不会把 Cookie 打印到日志。
如需退出此专用会话，可在该窗口内登出抖音。此模式不是重放网页签名：
带签名的详情请求由真实浏览器执行，F2 只下载返回的媒体地址，不会将账号 Cookie
和接口安全字段转发给媒体 CDN。不保证平台所有页面版本均可适配。

支持标准 `/video/ID`、`/note/ID` 链接、`v.douyin.com` 短链接，以及带数字 `modal_id` 的弹窗链接
（只下载弹窗对应的作品，不抓取主页），不支持主页批量下载。
视频下载后检查是否生成了非空 MP4；图文检查全部图片及其归档大小和 SHA-256。
未产生所需文件会报错；这些检查不代替完整媒体解码或音画完整性验证。

如需旧版 TikTok 下载、加工和视频号上传流水线，安装完整依赖并修改
`pipeline.py` 中的硬编码账号、路径和调度配置，准备下载目录后显式执行：

```powershell
& .\.venv\Scripts\python.exe .\main.py --pipeline
```

### 本地人物优化样片（实验）

这是传统图像处理，**不是 AI 换人/换脸**，不改变人物身份、身形或动作。
仅用于你有权改编的素材，不上传媒体，也不自动发布。
在下载依赖基础上安装独立的最小处理依赖，无需 GPU：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-portrait.txt
& .\.venv\Scripts\python.exe .\portrait_preview.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --start 0 --duration 8 --strength 0.6
```

默认 `--profile natural` 保留原有轻度效果。如需明显增强磨皮、肤色和面部提亮，改用：

```powershell
& .\.venv\Scripts\python.exe .\portrait_preview.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --start 0 --duration 8 --profile strong --strength 0.9
```

`strong` 使用更强的双边平滑、肤色去饱和及高光渐弱的亮度提升，仍不改变人物身份或脸型。
它在眼睛、嘴部及遮罩边缘使用保护过渡；不是整张画面美白。强效果可能显得偏白或失去肤质细节，
需看样片决定是否接受。`strength` 的范围仍是 `(0, 1]`，不需要突破上限。

程序自动检测正面人脸及合理的双眼位置，尝试排除商品包装误检；在人脸肤色遮罩内
轻度平滑、提升亮度，避开眼睛和嘴部中心。未检测到的帧不做处理。
这不是精准人像分割，可能漏检、误检或闪烁，必须人工审核。

默认在该视频目录的 `edits\portrait-时间戳\` 中输出：

- `original.mp4`：所选原片段；
- `retouched.mp4`：所选档位的优化版；
- `comparison.mp4`：左原片、右优化版，顶部独立标识，不遮挡原画面；
- `comparison.jpg`：有实际编辑的代表帧对比；
- `report.json`：档位及参数、原文件及输出 SHA-256、逐帧检测位置、编辑像素数、
  编辑像素的平均亮度/通道变化、解码校验结果及限制。变化指标在有损编码前计算。

样片统一为 24 fps、最大宽度 720 像素；视频重新编码为 H.264。
原片段如有声音，会先编码为 AAC，再原样复用到优化版和对比版；没有声音时不生成替代音轨。
遮罩外像素不变是在编码前校验的，最终有损编码仍可能改变这些区域的像素。
没有检测到可处理人脸时明确报错，不生成“成功”样片；不修改下载原片。
`--output` 可指定新的输出目录，已有目录拒绝覆盖；重新运行默认创建新的时间戳目录。
命令行和结构化报告可用于后续任务编排，目前不自动串接下载或上传，审核状态始终为 `needs_human_review`。

### 本地 AI 头部重绘静帧实验

`head_inpaint.py` 不再使用美颜滤镜，而是通过 Stable Diffusion Inpainting
重绘单张视频帧中的头脸/上部头发，尝试产生不同的虚构成年人物。
**这不是完整视频换人，不保证身份一致性、口型或商业可发布性。**
仅处理已获改编授权的素材。现有下载依赖基础上安装 CPU 生成环境：

```powershell
& .\.venv\Scripts\python.exe -m pip install "torch>=2.6,<3" --index-url https://download.pytorch.org/whl/cpu
& .\.venv\Scripts\python.exe -m pip install "torchvision>=0.21,<1" --index-url https://download.pytorch.org/whl/cpu
& .\.venv\Scripts\python.exe -m pip install -r requirements-generative.txt
& .\.venv\Scripts\python.exe .\head_inpaint.py prepare-model
```

模型为 [SD 1.5 Inpainting 社区镜像](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-inpainting)，
固定 revision `8a4288a76071f7280aedbdb3253bdb9e9d5d84bb`，许可为 CreativeML OpenRAIL-M；
模型卡将其定位为研究用途。本功能只作为本地原型，不代表已经完成发布用途的许可核查。
准备命令仅下载公开模型、配置及模型说明到被 Git 忽略的 `.models\sd15-inpainting`，
不读取视频，不上传任何素材。只下载 safetensors 权重，包含安全检查器；
文件大小和 SHA-256 会保存为本地模型清单，每次渲染先校验。

```powershell
& .\.venv\Scripts\python.exe .\head_inpaint.py render `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --steps 24 --seed 42 --threads 8
```

`render` 强制 CPU/float32、本地文件和 Hugging Face/Transformers 离线模式。
首次需联网下载模型，但推理不调用云服务。安全检查未通过或不可用时拒绝导出。
默认从前 15 秒的 24 个候选帧选取可靠的单人正脸大图；`--seconds` 可指定时间。
默认人物设定为自然生活化的虚构中国成年女性，黑发、细框眼镜；
这是生成设定，不是对输入人物国籍或身份的判断。`--prompt` 可覆盖人物描述，
`--seed` 可探索其他结果。

输出位于该视频的 `edits\head-still-时间戳\`，也可用 `--output` 指定新目录：

- `original.png`、`mask.png`：选定原帧及自动头部遮罩；
- `generated.png`、`comparison.png`：合成结果及左原图/右 AI 图对比；
- `model_input.png`、`model_generated_crop.png`：512×512 模型输入和未合成的局部生成结果；
- `report.json`：模型版本/许可、提示词、随机种子、CPU 耗时、安全检查、变化指标和各文件校验值。

合成采用 PNG，逐像素校验遮罩外不变，不修改原视频。变化不足会报错，而不是将原图当作生成成功；
像素变化只证明发生编辑，不证明换人质量。自动椭圆遮罩不是精确头发分割，
遮罩外的长发仍保留原样；可能存在发际线、颈部、耳饰、光照和透视衔接问题。
结果始终为 `needs_human_review`，必须审核后再考虑视频时序处理，不会自动发布。

#### 可见人物与穿搭重塑（静帧）

`--scope person` 使用本地 LRASPP 人物分割，将可见人物及小范围轮廓扩展区域交给重绘模型，
而不是只改脸。默认设定为自然清爽的虚构中国成年女性、简约长袖针织上衣和淡妆。
只改变画面可见部分，不补造被遮挡或画面外的身体，不保证改变后的姿势、手部或服装自然。

先下载 PyTorch 官方人物分割权重（约 12.5 MiB；只在准备时联网，不上传素材）：

```powershell
& .\.venv\Scripts\python.exe .\head_inpaint.py prepare-person
```

分割权重保存在 `.models\person-lraspp`，渲染前校验大小和 SHA-256，使用
`torch.load(..., weights_only=True)` 仅加载权重，渲染时不会自动联网下载缺失模型。
人物模式必须明确确认改编权，并提供商品/文字保护框，例如本视频 **0.5 秒帧专用**：

```powershell
& .\.venv\Scripts\python.exe .\head_inpaint.py render `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --scope person --seconds 0.5 --rights-confirmed `
  --protect-box 115,340,305,565 --protect-box 440,1125,280,155 `
  --protect-box 10,1180,155,100 --protect-box 345,970,375,245 `
  --steps 28 --seed 7
```

保护框格式为原帧像素 `x,y,width,height`，分别保护商品及抓握区域、右下角文字、
左下角标识、另一只手。**这些坐标是针对该帧人工确定的，不是通用商品检测或 OCR；**
换视频或时间必须重新确认保护范围。框内像素不会被合成结果覆盖，导出前校验完全一致。
矩形框过大会连旧衣服一起保留；可改用 `--protect-mask 路径.png` 提供贴合商品轮廓的
**原帧同尺寸、8 位灰度 PNG**（非黑像素受保护），也可与多个保护框合并。
该遮罩仍需人工确认覆盖全部商品、文字及抓握区域，不接受空白、尺寸不符或彩色/透明图片。
当前精细轮廓样例为 `edits\person-product-protection-05.png`，只适用于该视频 0.5 秒帧；
配合 `440,1125,280,155`、`10,1180,155,100`、`345,970,375,245`、
`270,335,55,110` 四个框，分别保留右下角文字、左下角标识、另一只手和冰箱上的标签。
同样校验整个编辑遮罩外不变。背景只能在分割边界附近随轮廓扩展改变，分割仍可能有误。
人物模式按裁剪比例使用最大边 768 像素的模型输入；报告记录实际分辨率。

除原有文件外，还保存 `protected.png` 和带独立顶栏 AI 标识的 `generated_labeled.png`；
`generated.png` 是便于逐像素核对的未加标识合成底图，展示时应优先使用带标识版本。
报告记录授权确认、保护框、提示词 token 数及是否截断、人物分割来源、保护区改动数和审核清单。
使用精细遮罩时还记录输入遮罩文件的大小和 SHA-256，最终合并保护区保存在 `protected.png`。
超过模型 token 上限时明确报错，避免关键描述静默丢失。

“合规”不是一次安全模型检查即可保证：需另行核对素材/模型许可、商品宣传依据、
平台 AI 内容标识及发布规则。默认使用非暴露日常穿搭，不仿冒具体真人，不自动发布；
不保证平台审核通过或审美满意，所有结果均需人工确认。

### Klein 原脸保护换造型实验（CPU 单帧）

该实验用于明显修改发型和衣服款式，而不是局部调色。使用独立环境，不升级现有下载环境：

```powershell
& .\.venv\Scripts\python.exe -m venv .venv-klein
& .\.venv-klein\Scripts\python.exe -m pip install torch==2.14.1+cpu --index-url https://download.pytorch.org/whl/cpu
& .\.venv-klein\Scripts\python.exe -m pip install -r requirements-klein.txt
& .\.venv-klein\Scripts\python.exe .\klein_edit.py prepare-model
```

固定 `black-forest-labs/FLUX.2-klein-4B` 的
`e7b7dc27f91deacad38e78976d1f2b499d76a294` 版本，Apache 2.0，下载组件约 14.9 GiB，
不下载仓库内其他格式的重复权重。渲染校验每个文件的 SHA-256，只读本地 safetensors。
float32 仅权重约需 30 GiB 内存；启动要求至少 36 GiB 当前可用内存，运行还需额外资源。
官方 GPU／CPU-offload 示例不是本机纯 CPU 性能保证。

先准备旧实验的本地安全检查模型 `head_inpaint.py prepare-model`，再渲染：

```powershell
& .\.venv-klein\Scripts\python.exe .\klein_edit.py render `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --seconds 0.5 --rights-confirmed `
  --edit-mask ".\Download\douyin\7679686176123636901\edits\klein-person-mask-refined-05.png" `
  --protect-mask ".\Download\douyin\7679686176123636901\edits\klein-protection-precise-05.png" `
  --output ".\Download\douyin\7679686176123636901\edits\klein-still-v1"
```

当前造型指令为低马尾与不透、长袖、圆领的简洁连衣裙上身。Klein 管线没有原生局部遮罩，
因此整张参考图先生成，随后只合成允许编辑的区域；默认模式下原人脸检测框始终强制受保护，
最终校验保护区和编辑区外逐像素不变。示例遮罩还人工保护了完整脸部、眼镜、商品、
抓握手、另一只手和文字，**只适用于该帧，不能当通用检测**。
保存带 AI 标识的预览、对比、模型原始输出、遮罩、耗时/进程峰值内存和报告。
复用原 SD 本地安全检查器（其 CreativeML OpenRAIL-M 条款仍需审核），失败不导出。
不上传素材，不自动发布；发际线、领口、手臂衔接及是否好看仍须人工验收，
程序跑通不代表造型合格，也不是已完成的视频换装。

若人工检查发现分割漏掉衣服图案，可修正该帧编辑遮罩后使用 `recompose`，
参数同上，另加 `--previous-result 原结果目录` 并换一个新 `--output` 目录；
不需要再次传 `--seconds`、`--rights-confirmed`。它核对原结果/视频哈希和原授权记录，
复用同一份 `model_output.png`，重新执行合成与本地安全检查，不重新运行生成模型。
报告记录父报告哈希及 `model_inference_repeated=false`；修遮罩不能保证发型或服装好看。

本机一次实际 CPU float32 实验：432×768、4 步，加载约 24.5 秒、推理约 152.9 秒，
生成进程峰值工作集约 32.7 GiB。衣服换款明显，但精确保留原脸/商品后的合成仍有旧衣料残留，
低马尾也未完整达标；`klein-still-v5` 是待精修的单帧实验，不是审美验收合格的成品。
这次只运行了一次生成模型，后续版本复用同一模型输出修正遮罩与合成。

#### 仅参考风格的新人物预览

`render --style-brief 简报.json` 显式启用人物重设计，不再强制保留原脸和眼镜；
不传此参数仍保持旧版原脸保护行为。新模式使用 576×1024 左右的原比例输出、4 步，
CPU 时间和内存开销高于上述旧实验，旧实验的耗时不能直接套用。
简报格式如下，图片路径相对简报目录解析：

```json
{
  "rights_confirmed": true,
  "reference_use": "style_only",
  "face_free_crops_reviewed": true,
  "style_description": "Long chestnut hair and a plain cocoa-brown crewneck top.",
  "references": [
    {"path": "reference.webp", "crop": [190, 820, 520, 520]}
  ]
}
```

只有实际具备改编权限、且人工确认裁剪不含人脸时才能填写这些确认值。
每次接受 1..2 张局部参考；`crop` 为 `[x,y,width,height]`，必须在图片范围内且至少 32×32。
程序只把裁剪后的头发/衣料风格板与视频原帧输入本地模型，不输入完整参考人像。
裁剪仍需人工审核，不是自动人脸排除；提示词请求新的虚构成年中国女性，不保证身份一定不同。
需要重新准备只保护商品、手部和文字的遮罩：至少 80% 原脸区域必须可编辑。
报告记录风格板、简报/参考图哈希和裁剪坐标；处理期间输入变化则拒绝导出。
新人物模式保留较宽的人物外轮廓羽化，但在商品/手部保护边缘使用较窄羽化，
避免把保护区周围的旧衣料一起留下；报告记录两种宽度。原脸保护模式仍使用原有合成行为。

例如在旧 `render` 命令上使用新的编辑/保护遮罩，追加
`--style-brief ".\Download\douyin\7679686176123636901\edits\klein-style-reference-v1.json"`，
并指定一个不存在的新输出目录。人物变化、商品边缘、人物身份区别及审美仍须人工审核。
`recompose` 按原报告模式保留或放开脸部，复用同一生成结果，并将外观审核状态重置为待审核。
这仍只是单帧预览，不是整段视频换人，也不自动发布。

本机新模式实测：576×1024、4 步、2 张无脸局部拼成风格板，报告中加载约 43.2 秒、
推理阶段约 278.5 秒，进程峰值工作集约 37.5 GiB。`klein-style-person-v3` 复用这一次
生成修正合成，保护区/编辑区外逐像素未变；瓶底和手腕仍有轻微衔接问题，面部偏平滑，
审核状态为 `needs_refinement`，尚未通过用户审美验收。

#### 保留参考人物身份的预览

若要使用同一位已授权人物，而不是另造新人物，在 `render` 中改用
`--identity-reference 参考人物图片`，不能同时传 `--style-brief`。
`--rights-confirmed` 此时同时确认原视频和参考人物的相应改编使用权；
下载成功或有 AI 标注并不能代替授权。程序要求参考图恰好检测到一张带双眼的正面人脸，
提取完整脸部及少量周边作为模型输入，保留裁剪图、坐标与原图哈希。
继续使用允许原脸变化的编辑/保护遮罩，输出到新的目录。

这一路径明确请求同一人物的五官、比例和神态，不再使用新虚构人物提示词；
但生成模型并非逐像素复制参考脸，也不保证身份完全一致。
报告中的 `identity_consistency_verified` 在人工确认前始终为 `false`。
安全检查、商品/手部/文字保护、源文件不变检查及不上传、不自动发布的限制保持不变。
仍须先审核单帧的相似程度、自然感和接缝，不能直接当整段视频身份一致性验证。

当前 `klein-identity-person-v1` 已实际输入完整参考脸部，576×1024、4 步，
报告中加载约 18.6 秒、推理阶段约 307.1 秒、峰值工作集约 33.2 GiB。
但眼、鼻及面部比例仍偏离参考，审核状态为 `needs_identity_refinement`，
**没有验证达到“同一个人”的要求**，不能以测试通过或提示词要求代替人物保真验收。

### 完整时长头部替换实验（本地 CPU）

`head_video.py` 将已审核范围的头部静帧扩展为逐镜头实验视频。
当前本地计划针对 `7679686176123636901`：复用首个正脸预览，为后续侧脸/转正脸生成关键帧，
商品特写保留原样。原片第 180 帧及两种生成方案均被安全检查拦下，未生成 v1 完整视频；
经用户确认，v2 将 `[155,203)` 的正脸特写替换为原片 `[56,104)` 的商品特写，
不改变全片时长和原音频。它不是任意视频可无人值守使用的自动换脸工具。
最终 v4 复用缓存并校正头部位置，转回正脸时直接复用已选正脸；
姿态切换不再叠加两张不同方向的人脸，仍可能出现明显跳变和旧发/背景修补痕迹。

```powershell
& .\.venv-klein\Scripts\python.exe .\head_video.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --plan ".\Download\douyin\7679686176123636901\edits\head-video-plan-v2.json" `
  --output ".\Download\douyin\7679686176123636901\edits\klein-head-video-v4" `
  --keyframes-dir ".\Download\douyin\7679686176123636901\edits\head-video-keyframes-v2" `
  --alignment ".\Download\douyin\7679686176123636901\edits\head-video-alignment-v1.json" `
  --reviewed-source-shots
```

使用已安装的 Klein/安全检查模型，不下载新模型、不上传、不自动发布。输出目录必须不存在。
计划与遮罩是本机人工准备的输入，不随仓库分发。计划必须确认授权及遮罩人工审核，
记录原视频 SHA-256、帧率、`approved_still`、`identity_crop: [x,y,width,height]`，
以及覆盖全片、无空隙和重叠的 `shots`。镜头范围为 `[start,stop)`；
商品镜头指定 `preserve: true`，人物镜头提供递增的 `keys`：
每项包含 `frame` 和原帧像素坐标的头部 `polygon`，或 `reuse` 指向已审核静帧。
另经用户确认的整段镜头替换，需设置 `shot_replacement_confirmed: true`，
并在该镜头指定 `replace_with_start`（同一原片内等长素材的起始帧）。
替换来源逐帧记入报告，不能与 `preserve` 或生成关键帧同时使用，也不计作头部局部修改。
身份条件来自所选实验人物的裁剪，不代表原参考人物相似度已经达标。

关键帧以哈希校验缓存，可在后续跟踪/编码失败后复用；运行中不要修改计划和输入。
更换计划需要新的缓存目录。DIS 双向光流逐帧累计坐标映射，镜头切换时重置，
转头段在不同姿态关键帧之间插值；跟踪面积或一致率不足时明确失败，不会静默跳过人物帧。
默认自动检查全部画面。原片商品镜头也会被生成图片检查器误报；只有人工核对原片内容、
且用户明确同意后，才能加 `--reviewed-source-shots`：保留/替换的原片镜头改由人工审核，
AI 改动帧仍逐帧自动检查，失败则停止。报告区分两种审核，不能宣称全部帧通过自动检查。
当前 v4 为 98 帧 AI 自动检查通过、147 帧原片人工审核。
`--alignment` 可提供人工审核对应点/头部轮廓，记录相似变换、旧发区背景修补和姿态切换帧；
对齐文件也记录哈希，已有结果生成后不要修改其输入文件，下一次调整应使用新文件。
头部编辑帧检查编码前遮罩外零改动；完整解码验证帧数、尺寸和帧率，并核对保留音轨的包哈希。
导出 `head-only.mp4`、`contact-sheet.png` 和逐帧报告，全部校验通过才提交结果目录。

**局限：**这是光流驱动的纹理变形，不是身份保真视频模型或口型同步模型。
侧脸到正脸的身份、眨眼/口型、头发接缝及审美仍需人工审核。
传播遮罩不等于自动衣服、手或遮挡分割；不要把编码前遮罩外不变当作语义保护已验证。
有损 MP4 编码也可能改变遮罩外像素。现有模型/安全组件的许可分别适用，
安全检查通过不代表平台批准或可直接发布。

### 保留原脸的自然穿搭配色预览

不满意生成式换人效果时，可先保留原人物与原衣服的真实纹理，只调整局部配色：

```powershell
& .\.venv\Scripts\python.exe .\clothing_preview.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --seconds 0.5 `
  --clothing-mask ".\Download\douyin\7679686176123636901\edits\clothing-soft-blue-mask-05.png" `
  --output ".\Download\douyin\7679686176123636901\edits\natural-clothing-v2"
```

只需 `requirements-portrait.txt`，不使用生成模型，也不上传素材。遮罩必须是原帧同尺寸、
非空的 8 位灰度 PNG，非黑像素指定衣服配色范围；覆盖已检测人脸时明确拒绝。
在 Lab 色彩空间柔化原蓝色色度、保留原亮度通道，不做磨皮、几何变形、发型或妆容修改，
也不生成新款式。程序核对人脸与遮罩外零改动，并输出原图、配色图、对比、遮罩和报告。
该示例遮罩是针对该视频 0.5 秒帧人工选定的蓝色针织区域，不是自动衣服检测；
商品、手和文字必须在遮罩外，其他帧需要重新核对。结果仍是待人工审美审核的静帧，
不是已经完成的自动视频换装，也不保证比原图好看。输出目录已存在时拒绝覆盖。

## 当前限制

- 平台页面和接口发生变化时，上传适配器可能需要同步更新。
- 部分上游依赖存在版本兼容要求，建议固定依赖版本并使用隔离环境。
- 项目尚未建立完整的自动化测试与 CI，生产使用前应进行小规模验证。
- API Key、Cookie 和账号配置不应提交到 Git 仓库。

## 项目价值

本项目重点验证的是跨工具、跨平台工作流的拆解与编排能力：如何把文件监听、媒体处理、LLM 调用、定时调度和浏览器自动化串成一条可观测、可恢复的流水线，包括任务边界设计、第三方系统适配、失败日志和后续扩展，而非单一的视频处理算法。其中"上游库需要打补丁才能兼容真实平台接口"这类工程细节，也正是自动化项目落地时最常见的隐性成本。

## 📂 更多项目

- [ai-highlight-clip](https://github.com/toki-plus/ai-highlight-clip) — 长视频智能初筛：Whisper 转写 + LLM 评分 + 人工终审，分钟级定位高光片段
- [ai-ttv-workflow](https://github.com/toki-plus/ai-ttv-workflow) — 文案到短视频的桌面工作流，关键节点保留人工确认
- [ai-video-workflow](https://github.com/toki-plus/ai-video-workflow) — 多模型 AIGC 视频生成流水线：文生图、图生视频、文生音乐的异步编排
- [ai-mixed-cut](https://github.com/toki-plus/ai-mixed-cut) — 素材库结构化与脚本重组的视频再创作工作流
- [ai-trader-for-mt4](https://github.com/toki-plus/ai-trader-for-mt4) — LLM×MT4 受控执行框架：工具约束、风控规则、状态管理与异步桥接
- [ai-trader-for-mt5](https://github.com/toki-plus/ai-trader-for-mt5) — 面向 MT5 的 AI 交易助手与 EA 工程化框架
- [auto-usps-tracker](https://github.com/toki-plus/auto-usps-tracker) — 跨境电商批量物流追踪与 Excel 报告自动化
- [AB-Video-Deduplicator](https://github.com/toki-plus/AB-Video-Deduplicator) — 基于高帧率抽帧混合的视频再创作实验工具
- [netease-downloader](https://github.com/toki-plus/netease-downloader) — 网易云音乐下载桌面应用：扫码登录、下载队列、ID3 元数据写入

## License

See [LICENSE](./LICENSE).
