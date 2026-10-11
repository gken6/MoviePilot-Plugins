# 来源与归属

豆瓣观看同步（`DoubanWatchSync`）由 gken6 维护。

感谢 [honue / MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins) 的[“豆瓣书影音档案”插件](https://github.com/honue/MoviePilot-Plugins/tree/main/plugins/doubanwatching)。本插件中读取 MoviePilot 已配置媒体服务器的非直连功能，参考该作者的实现改编，包括媒体服务器观看事件处理与豆瓣档案同步的原始思路。

独立直连 Emby 功能由 gken6 自主提出并设计：服务器独立添加、账号登录获取 Token / 用户 ID、多服务器轮询、启停、连接状态及影片/剧集数量展示，均不依赖 MP 已配置的媒体服务器。

在此基础上，当前版本增加了人工匹配与重新同步、无条目管理、写入结果校验、Cookie 获取顺序、账号/扫码登录、界面及仪表盘等改进。插件卡片作者显示当前维护者 gken6，不表示参考部分全部为当前维护者原创。

参考来源仓库采用 [GPL-3.0 许可证](https://github.com/honue/MoviePilot-Plugins/blob/main/LICENSE)。豆瓣观看同步保留该许可及上游代码归属，许可证全文见本目录 [LICENSE](LICENSE)。该声明适用于豆瓣观看同步，不改变仓库内其他插件的来源或许可。
