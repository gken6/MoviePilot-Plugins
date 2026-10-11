# MoviePilot 插件库

由 [gken6](https://github.com/gken6) 维护，适用于 **MoviePilot V3（>=3.0.0）**。

## 安装与更新

1. 打开 MoviePilot 的“插件”页面，进入插件仓库管理。
2. 添加仓库地址：

   ```text
   https://github.com/gken6/MoviePilot-Plugins
   ```

3. 刷新插件市场，搜索“豆瓣观看同步”，点击安装。
4. 打开插件设置，配置媒体服务器及豆瓣登录态，然后启用插件。

如果使用环境变量管理插件源，将上面的地址追加到 `PLUGIN_MARKET`；保留已有地址，用英文逗号分隔。后续版本可在插件市场更新。仓库的 `main` 分支包含市场索引与插件源码。

也可通过 GitHub 的 **Code → Download ZIP** 下载完整仓库。推荐使用插件市场安装，以便接收后续更新。

## 插件

| 插件 | 当前版本 | 功能 |
| --- | --- | --- |
| 豆瓣观看同步 | 2.3.3 | 将已经发生的观看同步到豆瓣，独立直连 Emby、多服务器管理、人工匹配与无条目排除 |
| TG万能签到 | 0.3.2 | TG 个人账号逐行执行命令、等待和按钮点击，定时任务与七天运行历史 |

### 豆瓣观看同步

感谢 [honue](https://github.com/honue/MoviePilot-Plugins) 的“豆瓣书影音档案”插件：读取 MP 已配置媒体服务器的非直连功能参考该作者改编；独立直连 Emby 部分由 gken6 自主提出并设计。

- 沿用 MoviePilot 已配置媒体服务器的事件处理和观看状态判定。
- 直连 Emby 独立管理：服务器名、地址、账号、密码，添加后自动获取 Token 和用户 ID。
- 显示连接状态、影片及剧集数量，支持多台服务器、编辑、删除和启停。
- 已同步和待同步记录可人工指定豆瓣编号并重新同步；影片只保留“看过”，剧集可选“在看 / 看过”。不提供“想看”。
- “无条目”内容停止自动匹配，以后可移回待同步或填写编号。
- 仪表盘展示观看档案、豆瓣标题和封面。
- 豆瓣写入失败保留提示和重试入口；更换编号时先写新条目，再尝试撤销旧标记。

同步时依次尝试插件保存的 Cookie、已启用的 CookieCloud、已配置的账号登录。账号遇到滑块验证或登录失败时通过 MP 发送二维码，扫码确认后保存 Cookie 并继续同步。直连账号只存放在本插件数据中，管理接口不返回密码、Token 或用户 ID。

撤销旧豆瓣标记必须能从官方页面核实接口；无法核实或执行失败时保留“旧标记待撤销”提示。此流程已有模拟测试，尚未完成真实豆瓣账号的端到端撤销验证。

详细说明见 [插件文档](plugins.v3/doubanwatchsync/README.md)，来源归属见 [NOTICE.md](NOTICE.md)。

### TG万能签到

使用自己的 Telegram API ID、API Hash 和手机号完成登录，按目标逐行设置命令，支持 `发送:`、`点击:`、`等待:`。可定时执行或立即执行，展示七天历史及每个目标的回复，支持 SOCKS5/HTTP 代理与 MP 通知。

新安装默认不启用，没有账号、目标或代理地址。详细登录步骤与命令示例见 [TG万能签到文档](plugins.v3/tguniversalsign/README.md)。

## 开发验证

```bash
python -m pip install pytest beautifulsoup4 Pillow
python tests/v3/doubanwatchsync/run_isolated.py
python tests/v3/tguniversalsign/run_isolated.py
node tests/v3/doubanwatchsync/frontend.mjs
```

后端测试模拟宿主存储、媒体服务器和豆瓣请求，不访问真实账号或修改观看记录。前端测试覆盖配置绑定、仪表盘刷新、状态选项、提交参数和失败提示。
