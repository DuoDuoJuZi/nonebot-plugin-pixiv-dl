<div align="center">
  <img src="https://github.com/Misty02600/nonebot-plugin-template/releases/download/assets/NoneBotPlugin.png" width="310" alt="NoneBot 插件图标">

  <h1>nonebot-plugin-pixiv-dl</h1>

  <p>基于 NoneBot2 的 Pixiv 搜索与下载插件</p>
</div>

## 📖 介绍

支持 Pixiv 插画，Ugoira 动图，漫画和小说的搜索，翻页与下载，通过 OneBot V11 发送结果

Ugoira 在用户层面属于图片，无需额外的动图搜索或下载命令

## 💿 安装

推荐使用 NB-CLI 安装

```bash
nb plugin install nonebot-plugin-pixiv-dl
```

<details>
<summary>其他安装方式</summary>

使用 pip 安装

```bash
pip install nonebot-plugin-pixiv-dl
```

从源码安装时，在插件目录执行

```bash
pip install .
```

使用 uv 时执行 `uv pip install .`

手动安装后，在机器人项目的 `pyproject.toml` 中追加插件，并确认已注册 OneBot V11 适配器

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_pixiv_dl"]
```

已有 `plugins` 列表时只追加插件加载名即可

</details>

## ⚙️ 配置

在机器人项目的 `.env` 文件中设置 Cookie，代理按需填写，Cookie 不包含 `Cookie:` 前缀

```dotenv
PIXIV_COOKIE="PHPSESSID=替换为自己的会话值"
PIXIV_PROXY="http://127.0.0.1:7890"
```

| 配置项                            | 必填 | 默认值 | 说明                                           |
| :-------------------------------- | :--: | :----: | :--------------------------------------------- |
| `PIXIV_COOKIE`                    |  是  |   空   | Pixiv 登录 Cookie                              |
| `PIXIV_R18`                       |  否  | `true` | 是否允许搜索和下载 R18 作品                    |
| `PIXIV_SEARCH_LIMIT`              |  否  |  `20`  | 每批各分类的搜索结果上限                       |
| `PIXIV_SEARCH_PREVIEW`            |  否  | `true` | 是否显示插画和漫画的搜索预览                   |
| `PIXIV_PREVIEW_CONCURRENCY`       |  否  |  `4`   | 预览处理并发数，范围为 `1` 至 `16`             |
| `PIXIV_PREVIEW_MAX_EDGE`          |  否  | `512`  | 预览最长边像素上限，范围为 `64` 至 `1024`      |
| `PIXIV_UGOIRA_PREVIEW_MAX_EDGE`   |  否  | `256`  | 动图 GIF 预览最长边，范围为 `64` 至 `512`      |
| `PIXIV_UGOIRA_PREVIEW_MAX_FRAMES` |  否  |  `60`  | 动图 GIF 预览最多保留帧数，范围为 `1` 至 `120` |
| `PIXIV_FORWARD_MAX_MESSAGES`      |  否  |  `20`  | 每包转发的节点上限，最小为 `2`                 |
| `PIXIV_DOWNLOAD_MAX_PAGES`        |  否  |  `30`  | 漫画单次下载的页数上限                         |
| `PIXIV_NOVEL_MAX_CHAPTERS`        |  否  |  `50`  | 小说系列单次下载的章节上限                     |
| `PIXIV_TIMEOUT`                   |  否  |  `20`  | 网络请求超时秒数                               |
| `PIXIV_PROXY`                     |  否  |   空   | 网页和图片共用的 HTTP 或 SOCKS 代理            |
| `PIXIV_FIXED_IP`                  |  否  |   空   | Pixiv 主站固定 IP，显式代理优先                |

代理与固定 IP 均未设置时使用系统 DNS 或环境代理

本地 NapCat API 应绕过系统代理，机器人进程的环境变量可设置为

```dotenv
NO_PROXY=127.0.0.1,localhost,::1
```

NapCat 位于局域网时，将其 IP 也加入 `NO_PROXY`，此设置用于机器人访问协议端，与 `PIXIV_PROXY` 控制的 Pixiv 请求不同

## 🎉 使用

以下指令均可在群聊和私聊使用

| 指令                 | 说明                                                   |
| :------------------- | :----------------------------------------------------- |
| `/px搜索图片 关键词` | 搜索普通插画和 Ugoira，动图默认显示小尺寸 GIF 预览     |
| `/px搜索漫画 关键词` | 搜索漫画，默认显示预览                                 |
| `/px搜索小说 关键词` | 搜索小说信息                                           |
| `/px搜索 关键词`     | 分别搜索图片，漫画和小说                               |
| `/px搜索下一页`      | 继续本人当前搜索的全部可用分类                         |
| `/px搜索图片下一页`  | 继续图片搜索，包含普通插画和 Ugoira                    |
| `/px搜索漫画下一页`  | 继续漫画搜索                                           |
| `/px搜索小说下一页`  | 继续小说搜索                                           |
| `/px下载图片 PID`    | 下载插画原图，Ugoira 则发送原始帧 ZIP 和全尺寸兼容 MP4 |
| `/px下载漫画 PID`    | 下载漫画原图                                           |
| `/px下载小说 ID`     | 下载小说或所在系列的章节 TXT 文件                      |
| `/px下载 PID`        | 自动识别作品类型并下载                                 |

参数前可省略空格，例如 `/px搜索图片风景` 和 `/px下载图片123456`

- 搜索结果以合并转发发送，插画和漫画默认附带低清预览，预览失败时仅展示作品信息
- 插画和漫画按页发送原图，小说按章节发送 TXT 文件，超出转发节点上限自动分包
- 首次搜索成功后，记录保留 120 秒，翻页不会延长，过期后需重新搜索
- 聚合搜索使用分类翻页命令成功后，仅继续该分类，切换分类需重新搜索

### Ugoira 下载

先以普通消息发送作品信息，再上传 `{PID}_ugoira.zip`，随后生成并发送 `{PID}_ugoira.mp4` 视频

- ZIP 从 Pixiv `originalSrc` 流式下载一次，直接发送原文件，不修改，不重新压缩，用于保存完整原始帧
- MP4 使用同一个原始 ZIP 的全部帧，不缩放，不裁剪，不抽帧，奇数宽高仅向右侧和底部补齐到偶数
- MP4 是观看用的兼容压缩版本，使用 `libx264`，`yuv420p`，`CRF 20`，`veryfast` 和 `+faststart`，存在有损压缩和色度降采样，不是无损 RGB 视频
- 按 Pixiv 每帧 `delay` 生成 VFR 时间轴，允许少量时间量化，不固定为 30 FPS 或 60 FPS，也不统一声明为 1000 FPS
- 解码，滤镜和编码线程均为 `1`，一个机器人进程同时只转换一个 Ugoira，转换上限为 600 秒
- `imageio-ffmpeg` 提供可执行文件，通常无需手动安装 FFmpeg，平台缺少对应 wheel 时会明确提示
- 群聊通过 `upload_group_file`，私聊通过 `upload_private_file` 上传 ZIP，文件名只使用 PID，ZIP 不放入合并转发，MP4 通过 OneBot 视频消息发送
- ZIP 和视频分别等待最多 180 秒的 API 回执，ZIP 上传失败仍尝试视频，超时表示发送结果未确认，请检查聊天记录，插件不会自动重发
- 发送结果未确认时保留临时资源 300 秒后清理，发送明确完成或失败时正常清理，机器人正常退出时回收暂存资源

搜索 GIF 独立使用 `src`，默认最长边为 256 像素，最多 60 帧，均匀采样时合并跳过帧的延时，R18 GIF 每帧缩放后模糊，处理失败时安全降级为模糊静态预览或仅展示元数据

## 📌 注意事项

- R18 搜索预览始终模糊，设为 `PIXIV_R18=false` 后过滤并禁止下载 R18 作品
- 合并转发需协议端支持，小说文件还要求支持 NapCat 的文件消息扩展，Ugoira ZIP 需要群聊或私聊文件上传 API，协议端必须能够访问插件生成的本地临时文件
- MP4 采用常见兼容格式，实际上传和播放仍受 NapCat 版本，QQ 客户端及文件大小限制影响，自动化测试不等同于真实 QQ 播放验证
- 搜索或下载失败时查看控制台日志，检查 Cookie 和网络，固定 IP 失效时清空 `PIXIV_FIXED_IP`

## 📃 许可证

本项目采用 [MIT](./LICENSE) 许可证

## 🙏 特别鸣谢

- [pixiv-api-http](https://github.com/Dituon/pixiv-api-http)，提供 Pixiv 网页接口实现参考
- [nonebot-plugin-jmdownloader](https://github.com/Misty02600/nonebot-plugin-jmdownloader)，提供 README 结构参考
