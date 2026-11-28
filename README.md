# AI 模型雷达 · 在线面板

固定访问地址：

**https://bokaiz1337.github.io/ai-model-radar/**

这个仓库是 `~/.aimonitor` 本地程序的在线发布副本。

- **主通道**：本机每次更新后直接把 dashboard 推到 `gh-pages` 分支（15 分钟 / 3 小时节奏）
- **兜底通道**：GitHub Actions 定时抓取 models.dev、LiteLLM、OpenRouter、USD/CNY 汇率，生成 `index.html` 并推送到 `gh-pages`

页面本身每 5 分钟自动刷新，右上角可关闭。现在发布走 `gh-pages` 分支，不再依赖 GitHub Pages artifact。

数据仅包含公开模型目录与价格，不含任何密钥。
