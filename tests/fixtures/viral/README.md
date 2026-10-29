# 真实响应离线样本

来源：`.codex-runs/viral-compare-others/data/`，2026-10-29 调研采样。
同名文件裁剪为解析需要的字段；balance-402 来自 dy-likes-page2-0 的 error JSON。
仅保留作品公开文字/指标/日期、作者公开 ID/粉丝和必要分页信息；去掉请求头、访问令牌、签名链接、设备/定位/调试信息和无关嵌套字段。分页 search_id/backtrace 是公开响应延续字段，不是 API key。
原始日期与指标保持采样值，测试使用固定时间；不作为当前线上可用性证明。
