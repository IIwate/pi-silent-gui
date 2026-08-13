# 砍成七个可调用工具

Status: Accepted

调用方要的是启动、等到窗、截图、点、输入、按键、杀掉。身份、协议帧、音频策略和批量 steps 留在内部，不再出现在工具参数和成功响应里。

私有桌面和 Job 收树保留：这是不抢用户屏幕的原因。文本输入走 `WM_CHAR`，不引入 UIA。

## Considered Options

- 继续加厚 `silent_message` + steps：调用方仍要学互斥规则，且还是不能打字。
- 拆成七个工具：每个工具一件事，agent 能直接填参数。

## Consequences

- `silent_message`、`clean_env`、`audio_device_policy` 不再是工具参数。
- 成功响应只保留调用方下一步用得上的字段；内部仍用 Job / desktop / token 做清理。
