<div align="center">
  <img src="https://github.com/Misty02600/nonebot-plugin-template/releases/download/assets/NoneBotPlugin.png" width="310" alt="NoneBot 插件图标">

  <h1>nonebot-plugin-pixiv-dl</h1>

  <p>基于 NoneBot2 的 Pixiv 搜索与下载插件</p>
</div>

## 📖 介绍

通过 Pixiv Web AJAX 搜索插画，漫画和小说，使用 OneBot V11 合并转发发送结果

图片和漫画搜索会下载低清预览，使用随插件安装的 Pillow 在内存中缩放并模糊 R18 预览

支持下载插画和漫画原图，以及将小说正文保存为 UTF-8 TXT 文件

## 💿 安装

推荐使用 NB-CLI 安装

```bash
nb plugin install nonebot-plugin-pixiv-dl
```

也可以使用 pip 安装

```bash
pip install nonebot-plugin-pixiv-dl
```

<details>
<summary>从本地源码安装</summary>

在插件目录执行

```bash
pip install .
```

使用 uv 时执行

```bash
uv pip install .
```

</details>

在 NoneBot2 项目的 `pyproject.toml` 中，将插件加载名加入 `[tool.nonebot]` 的 `plugins` 列表

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_pixiv_dl"]
```

已有 `plugins` 配置时只需追加 `nonebot_plugin_pixiv_dl`，同时确保项目已注册 OneBot V11 适配器

`nonebot-plugin-pixiv-dl` 是 PyPI 包名，插件加载名为 `nonebot_plugin_pixiv_dl`

## ⚙️ 配置

在机器人项目的 `.env` 文件或环境变量中设置 Pixiv Cookie

```dotenv
PIXIV_COOKIE="PHPSESSID=替换为自己的会话值"
PIXIV_PROXY="http://127.0.0.1:7890"
```

`PIXIV_COOKIE` 只填写浏览器请求中的 Cookie 值，不要包含 `Cookie:` 前缀

`PIXIV_PROXY` 为可选配置，可填写可用的 HTTP 或 SOCKS 代理地址

| 配置项                       | 必填 | 默认值 | 说明                                                        |
| :--------------------------- | :--: | :----: | :---------------------------------------------------------- |
| `PIXIV_COOKIE`               |  是  |   空   | Pixiv Web 登录 Cookie                                       |
| `PIXIV_R18`                  |  否  | `true` | 是否允许搜索和下载 R18 作品                                 |
| `PIXIV_SEARCH_LIMIT`         |  否  |  `20`  | 每个分类最多返回的搜索结果数                                |
| `PIXIV_SEARCH_PREVIEW`       |  否  | `true` | 是否显示搜索预览，关闭后仅发送元数据且不下载预览            |
| `PIXIV_PREVIEW_CONCURRENCY`  |  否  |  `4`   | 同时下载并处理预览的数量上限，范围为 `1` 至 `16`            |
| `PIXIV_PREVIEW_MAX_EDGE`     |  否  | `512`  | 预览最长边的像素上限，范围为 `64` 至 `1024`，缩放保持宽高比 |
| `PIXIV_FORWARD_MAX_MESSAGES` |  否  |  `20`  | 每包合并转发的节点上限，最小为 `2`                          |
| `PIXIV_DOWNLOAD_MAX_PAGES`   |  否  |  `30`  | 漫画单次最多下载的页数                                      |
| `PIXIV_NOVEL_MAX_CHAPTERS`   |  否  |  `50`  | 小说系列单次最多下载的章节数                                |
| `PIXIV_TIMEOUT`              |  否  |  `20`  | 单次网络请求的超时秒数                                      |
| `PIXIV_PROXY`                |  否  |   空   | Pixiv 网页和图片请求共用的代理地址                          |
| `PIXIV_FIXED_IP`             |  否  |   空   | Pixiv 主站使用的固定 IP 地址                                |

未配置 `PIXIV_PROXY` 和 `PIXIV_FIXED_IP` 时，插件会读取 `HTTPS_PROXY`，`ALL_PROXY` 等环境代理变量

仅配置 `PIXIV_FIXED_IP` 时，只有 `www.pixiv.net` 的 TCP 连接使用该地址，图片 CDN 仍按原域名连接

同时配置 `PIXIV_PROXY` 和 `PIXIV_FIXED_IP` 时优先使用代理，固定 IP 变化后需要自行更新

插件仅向 `www.pixiv.net` 发送 Cookie，图片请求不会携带 Cookie

## 🎉 使用

### 指令表

| 指令                 |    范围    | 说明                                  |
| :------------------- | :--------: | :------------------------------------ |
| `/px搜索图片 关键词` | 群聊和私聊 | 搜索插画并返回元数据和第一页预览      |
| `/px搜索漫画 关键词` | 群聊和私聊 | 搜索漫画并返回元数据和第一页预览      |
| `/px搜索小说 关键词` | 群聊和私聊 | 搜索小说并返回元数据                  |
| `/px搜索 关键词`     | 群聊和私聊 | 分别返回插画，漫画和小说的搜索结果    |
| `/px搜索下一页`     | 群聊和私聊 | 继续本人当前搜索的全部可用分类        |
| `/px搜索图片下一页` | 群聊和私聊 | 继续图片搜索，聚合搜索成功后锁定图片  |
| `/px搜索漫画下一页` | 群聊和私聊 | 继续漫画搜索，聚合搜索成功后锁定漫画  |
| `/px搜索小说下一页` | 群聊和私聊 | 继续小说搜索，聚合搜索成功后锁定小说  |
| `/px下载图片 PID`    | 群聊和私聊 | 下载插画原图并逐页发送                |
| `/px下载漫画 PID`    | 群聊和私聊 | 下载漫画原图并逐页发送                |
| `/px下载小说 ID`     | 群聊和私聊 | 下载独立小说或所在系列的章节 TXT 文件 |
| `/px下载 PID`        | 群聊和私聊 | 查询作品详情后识别插画，漫画或小说    |

指令以 `/px` 开头，分类与参数之间可以省略空格，例如 `/px搜索图片风景` 和 `/px下载图片123456`

搜索结果每个作品占一个合并转发节点，图片和漫画节点包含标题，作者，ID，标签等元数据及一张第一页预览，小说保持纯元数据，聚合搜索分别发送三个分类的聊天记录

分页记录仅保存在内存，按机器人，群聊或私聊环境及搜索用户隔离，首次搜索成功后固定保留 120 秒，翻页不会续期，重新搜索立即替换旧记录，即使新搜索失败也不会恢复旧记录

聚合搜索使用 `/px搜索下一页` 时仅请求仍有后续页的分类，各分类页码独立推进，失败分类保留原页码供重试，初次搜索失败的分类需重新发起搜索

聚合搜索中使用 `/px搜索小说下一页` 等分类命令，只有该分类下一页成功获取并解析后才锁定，之后通用翻页也仅继续该分类，切换分类需要重新搜索，无下一页或请求失败不会锁定

翻页复用相同的预览和转发流程，提示中的页码为 Pixiv 实际页码，与合并转发分包编号无关，结果上限较高时沿用跨 Pixiv 页补足本批结果的行为，下一批从实际最后读取页的下一页开始，不缓存整个结果集

分页优先采用接口的 `lastPage` 或 `total` 信息，只有缺少分页信息时才按原始响应每页 60 条判断，过滤 R18 后的数量和转发节点数不参与判断，空页会结束该分类

预览直接使用搜索响应中的 `url`，`thumb`，`thumbnail` 或 `small` 低清地址，不查询额外作品详情或其他页面，不使用原图和未裁剪的常规图片，无可用地址或单张处理失败时只展示该作品元数据

预览以质量为 `80` 的 JPEG 在内存中编码，透明部分使用白底，R18 始终使用轻度高斯模糊，半径为缩放后短边的 `1%`，限制在 `1` 至 `3` 像素，不提供关闭模糊的配置

下载结果以元数据为首节点，插画和漫画每页一张图片，小说每章一个 TXT 文件，超过节点上限会继续分包

搜索或下载时会发送临时状态消息，发送结果时尝试撤回，没有搜索结果时会直接发送提示

### 使用限制

- `PIXIV_R18=false` 时，搜索会过滤 R18 作品，下载会拒绝 R18 作品
- `PIXIV_R18=true` 时允许搜索 R18 作品，其搜索预览始终模糊，模糊或编码失败时仅发送元数据
- 暂不支持下载 Pixiv 动图
- 小说文件通过 NapCat 支持的 `file` 扩展消息段发送，实现端需要支持合并转发中的文件消息段，并能访问插件生成的本地临时文件
- 合并转发依赖实现端支持 `send_group_forward_msg` 或 `send_private_forward_msg` 扩展动作
- 群聊文件发送还可能受到 QQ 文件容量限制

## 🔧 常见问题

- 搜索或下载失败时，查看控制台中的 Pixiv 请求路径，HTTP 状态和异常信息
- Cookie 缺失或失效时，更新 `PIXIV_COOKIE`
- 图片下载失败时，检查 `i.pximg.net` 的网络连接或代理设置
- 固定 IP 无法连接时，清空 `PIXIV_FIXED_IP` 以恢复系统 DNS
- 小说文件发送失败时，确认实现端支持合并转发中的 `file` 消息段，且能访问本地临时文件

## 🙏 特别鸣谢

- [pixiv-api-http](https://github.com/Dituon/pixiv-api-http)，提供 Pixiv 网页接口实现参考
- [nonebot-plugin-jmdownloader](https://github.com/Misty02600/nonebot-plugin-jmdownloader)，提供 README 结构参考
