# astrbot_plugin_feiyu_browse

给 AstrBot 一个**只读**的浏览器：用你本机浏览器的真实登录态打开网页、读正文、截图，不点击、不填表、不动你的标签页。

## 特性

- **只读安全**：只做 `open / read / screenshot`，没有点击与输入，不会误触别人页面上的按钮
- **真实登录态**：复用已登录的浏览器实例，会员内容、需要登录的文档都能读
- **正文提取**：返回可读文本而不是一坨 HTML，默认截到 `max_chars` 字，省上下文
- **可选联网搜索**：填了 `search_key` 就能让模型用 `web_search`
- **自动识别链接**：群里有人甩链接，可以自动读一段摘要（可关）

## 安装

1. 装 [browser-skill](https://github.com/) 并确保 `bsk` 命令可用，或直接填 `bsk_path`
2. 目录放到 `astrbot/data/plugins/`，本插件无第三方依赖
3. WebUI 插件配置里填 `bsk_path`（可选）、`browser`（可选）
4. 重启 AstrBot

## 配置

| 键 | 说明 |
| --- | --- |
| `bsk_path` | `bsk.exe` 绝对路径；留空走 PATH |
| `browser` | 固定浏览器实例 id；留空自动取第一个在线实例 |
| `timeout` | 单次操作超时，默认 120 秒 |
| `max_chars` | 返回正文最大字数，默认 3000 |
| `search_key` / `search_api` | 联网搜索的 key 与地址，留空则关闭搜索工具 |

## 指令与工具

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| 指令 | `/看网页 <url>` | 读正文 |
| 指令 | `/网页截图 <url>` | 截图发回 |
| 工具 | `read_webpage` / `screenshot_webpage` | 同上，给模型调用 |
| 工具 | `browse_open` / `browse_act` | 打开页面 / 有限交互（需谨慎） |
| 工具 | `web_search` | 需配置 `search_key` |

## 常见问题

**报找不到 bsk**
`bsk_path` 填错或没装。填绝对路径后重启插件。

**读了但内容为空**
页面是纯 JS 渲染且没等够时间，调大 `timeout`；或该页面需要登录而你的浏览器实例没登录。

## 许可

MIT，见 [LICENSE](LICENSE)。
