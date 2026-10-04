<div align="center">
  <img src="https://github.com/Misty02600/nonebot-plugin-template/releases/download/assets/NoneBotPlugin.png" width="310" alt="NoneBot 插件图标">

  <h1>nonebot-plugin-pixiv-dl</h1>

  <p>基于 NoneBot2 的 Pixiv 搜索与下载插件</p>
</div>

## 📖 介绍

支持 Pixiv 插图，漫画和小说的搜索，翻页与下载，通过 OneBot V11 发送结果

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

在机器人项目的 `.env` 文件中配置，`PIXIV_COOKIE` 必填，其余配置可选，Cookie 不包含 `Cookie:` 前缀

```dotenv
PIXIV_COOKIE="PHPSESSID=替换为自己的会话值"
PIXIV_PROXY="http://127.0.0.1:7890"
```

| 配置项 | 默认值 | 说明 |
| :--- | :---: | :--- |
| `PIXIV_COOKIE` | 空 | Pixiv 登录 Cookie |
| `PIXIV_R18` | `true` | 是否允许搜索和下载 R18 作品 |
| `PIXIV_SEARCH_LIMIT` | `20` | 每批各分类的搜索结果上限 |
| `PIXIV_SEARCH_PREVIEW` | `true` | 是否显示插图和漫画的静态或动图预览 |
| `PIXIV_PREVIEW_CONCURRENCY` | `4` | 静态预览处理并发数，范围为 `1` 至 `16` |
| `PIXIV_PREVIEW_MAX_EDGE` | `512` | 静态预览最长边像素上限，范围为 `64` 至 `1024` |
| `PIXIV_UGOIRA_PREVIEW_MAX_EDGE` | `256` | 动图 GIF 预览最长边像素上限，范围为 `64` 至 `512` |
| `PIXIV_UGOIRA_PREVIEW_MAX_FRAMES` | `60` | 动图 GIF 预览帧数上限，范围为 `1` 至 `120` |
| `PIXIV_FORWARD_MAX_MESSAGES` | `20` | 每包转发的节点上限，最小为 `2` |
| `PIXIV_DOWNLOAD_MAX_PAGES` | `30` | 漫画单次下载的页数上限 |
| `PIXIV_NOVEL_MAX_CHAPTERS` | `50` | 小说系列单次下载的章节上限 |
| `PIXIV_TIMEOUT` | `20` | 网络请求超时秒数 |
| `PIXIV_PROXY` | 空 | Pixiv 请求代理，支持 HTTP 或 SOCKS |
| `PIXIV_FIXED_IP` | 空 | Pixiv 主站固定 IP，显式代理优先 |

代理与固定 IP 均未设置时使用系统 DNS 或环境代理

## 🎉 使用

以下指令均可在群聊和私聊使用，插图指令同时支持动图

| 指令 | 说明 |
| :--- | :--- |
| `/px搜索图片 关键词` | 搜索插图，默认显示预览 |
| `/px搜索漫画 关键词` | 搜索漫画，默认显示预览 |
| `/px搜索小说 关键词` | 搜索小说信息 |
| `/px搜索 关键词` | 分别搜索插图，漫画和小说 |
| `/px搜索下一页` | 继续本人当前搜索的全部可用分类 |
| `/px搜索图片下一页` | 继续插图搜索 |
| `/px搜索漫画下一页` | 继续漫画搜索 |
| `/px搜索小说下一页` | 继续小说搜索 |
| `/px下载图片 PID` | 下载插图，动图发送 ZIP 和 MP4 |
| `/px下载漫画 PID` | 下载漫画原图 |
| `/px下载小说 ID` | 下载小说或所在系列的章节 TXT 文件 |
| `/px下载 PID` | 自动识别作品类型并下载 |

参数前可省略空格，例如 `/px搜索图片风景` 和 `/px下载图片123456`

- 搜索结果以合并转发发送，插图和漫画默认附带预览，动图使用 GIF，无可用预览时仅展示作品信息
- 静态插图和漫画按页发送原图，小说按章节发送 TXT 文件，超出转发节点上限自动分包
- 首次搜索成功后，记录保留 120 秒，翻页不会延长，过期后需重新搜索
- 聚合搜索使用分类翻页命令成功后，仅继续该分类，切换分类需重新搜索

### 动图下载

- 原始帧 ZIP 用于保存完整原始资源
- MP4 视频采用有损压缩，用于直接观看
- FFmpeg 由插件依赖提供，通常无需额外安装

## 📌 注意事项

- R18 搜索预览始终模糊，设为 `PIXIV_R18=false` 后过滤并禁止下载 R18 作品
- 合并转发和文件上传需协议端支持，小说文件使用 NapCat 文件消息扩展，协议端需能访问插件生成的本地临时文件
- MP4 上传和播放受协议端，QQ 客户端及文件大小限制影响
- 发送结果确认超时时先检查聊天记录，再决定是否重试
- 搜索或下载失败时查看控制台日志，检查 Cookie 和网络，固定 IP 失效时清空 `PIXIV_FIXED_IP`

## 📃 许可证

本项目采用 [MIT](./LICENSE) 许可证

## 🙏 特别鸣谢

- [pixiv-api-http](https://github.com/Dituon/pixiv-api-http)，提供 Pixiv 网页接口实现参考
- [nonebot-plugin-jmdownloader](https://github.com/Misty02600/nonebot-plugin-jmdownloader)，提供 README 结构参考
