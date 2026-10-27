# Changelog


## 1.2.4 — 2026-10-25

- 工具收进统一入口 `feiyu(action)`（搜 / 读网页 / 截图）；原来五个 `@filter.llm_tool` 撤下，只留管理员调试用的 `page_do`。读网页改为优先取 `document.body.innerText`，读不到才退回无障碍树。


## 1.2.3 — 2026-10-25

- 工具不再直接往聊天里塞结果：web_search / read_webpage / browse_open / browse_act 现在把内容交回你，由你自己说，不再出现「一坨搜索结果」直接发到群里的情况。
- screenshot_webpage 改成「发图 + 交回一句话」，你知道图发出去了，不用再猜。
- 修复 search_api 配置不生效（kit 收 api 参数），搜索接口地址可以自定义了。
- 链接后面紧贴着写的中文（「…&vd_source=xxx给你看看这个」）不再被算进 URL。

## 1.2.1 (2026-10-25)

- 搜索接口改用 _feiyu_kit.deepseek_search。
- 新增 `_feiyu_kit.py`（vendor 方式共用，`sync_kit.py` 同步），功能与配置键不变。

## 1.2.0 — 2026-10-25

- `web_search` 结果里带上**链接、来源站、发布时间**，AI 可以顺着链接再用 `read_webpage` 读全文
- 新增 `count` 参数（1-8 条），摘要从 120 字扩到 140 字并清掉 Markdown 杂音
- 同一关键词 10 分钟内走缓存，接口抖动自动重试一次
- 接口彻底不可用时明确说「搜不动」，不再返回残缺结果

## 1.1.0 (2026-10-25)

首个可对外发布的版本。

### 新增
- `_conf_schema.json`：配置全部搬到 AstrBot 插件配置页，带分组标题与条件显示
- `README.md` / `CHANGELOG.md` / `LICENSE`(MIT) / `.gitignore` / `requirements.txt`

### 变更
- 移除源码里的机器专属路径与本机密钥，改为配置项（缺失时给出可读的提示而不是崩溃）
- 移除已废弃的 `@register(...)` 装饰器，改由 `metadata.yaml` 描述插件
- 配置读取改为「插件配置优先、`data/plugin_data/<name>/config.json` 兜底」，空值不覆盖
- 运行期自动探测到的值（如浏览器实例）会写回 config.json 持久化

### 1.0.0
- 内部自用版本，无对外文档。

## 1.2.2 (2026-10-25)

- 搜索走 kit 新版：支持配置里的 search_api、过滤非 dict 结果、超时回到 30s。
- 插件侧 import 前清 `sys.modules['_feiyu_kit']` 缓存，改用新 kit 不必整进程重启也能生效（稳妥起见仍建议重启）。
