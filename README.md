# pi-silent-gui

Windows 上给 Pi 用的窄 GUI 冒烟工具。在私有桌面里启动程序，不抢你的屏幕和光标。

适合可信程序的后台冒烟。它不是沙箱，也不是通用 computer-use。

## 能做什么

1. `silent_spawn` — 启动程序，返回 `session_id`
2. `silent_wait` — 等到匹配窗口。成功和失败都带回 Session 快照：`alive`、`exit_code`、全部顶层窗
3. `silent_capture` — 截一张和窗口一样大的 PNG，并把同一张图作为附件回给模型。点击坐标以这张图的左上角为 `(0,0)`
4. `silent_click` — 按 PNG 坐标点左键。同一点可传 `count` 连点
5. `silent_type` — 往当前焦点控件打字
6. `silent_key` — 按 `return` / `tab` / `escape` 这类有名字的键。同一键可传 `count` 连按；inject 会话还能传 `hold_ms` 长按（如按住 `control` 跳过）
7. `silent_kill` — 杀掉进程树并删掉 session 临时目录

## 安装

Windows 10+、Node.js >= 20.3、Python >= 3.10，以及 `pycaw`：

```bash
python -m pip install pycaw==20251023
pi install npm:@iiwate/pi-silent-gui
```

可用 `PI_SILENT_GUI_PYTHON` 指定解释器。装完执行 `/reload`。

## 用法

下面是工具参数，不是 shell 命令。

启动：

```json
{ "exe": "app.exe", "cwd": "C:/path/to/app", "args": ["--example"] }
```

保存 `session_id`，然后 `silent_wait` 等到窗口。快照里的 `windows` 是当前全部顶层窗；后续截图或点击仍用选中的那个 `hwnd`。

截图：

```json
{ "session_id": "..." }
```

点客户区中央（`client` 来自截图响应）：

```json
{ "session_id": "...", "x": 120, "y": 80 }
```

同一点连点：

```json
{ "session_id": "...", "x": 120, "y": 80, "count": 5 }
```

输入：

```json
{ "session_id": "...", "text": "hello" }
```

按键：

```json
{ "session_id": "...", "key": "return" }
```

同一键连按（中间默认隔 300ms）：

```json
{ "session_id": "...", "key": "return", "count": 8 }
```

清理：

```json
{ "session_id": "..." }
```

自定义截图路径默认不覆盖；要覆盖就传 `overwrite: true`。

## 输入模式

`silent_spawn` / `silent_wait` 会返回 `input_mode`：

- `message`（默认）：点击/按键走窗口消息，只对认窗口消息的引擎有效。很多轮询输入的引擎（Unity / DirectInput / RawInput，galgame 常见）不吃这套——连点两三下界面没变就该停下报告，而不是硬试。
- `inject`：给目标注入 payload DLL，让它的输入轮询改读一块共享内存；此时点击、按键、`hold_ms` 长按对轮询引擎才真正生效。

开启方式：给 `silent_spawn` 传 `inject_dll64` 路径参数（32 位目标用 `inject_dll32`）；懒得每次传，就设环境变量 `PI_SILENT_GUI_INJECT_DLL64` / `PI_SILENT_GUI_INJECT_DLL32` 当默认，**参数优先于环境变量**。两者都没有就一直是 `message`。payload 按 `src/backend/pi_silent_input.h` 的契约写；注入失败一律降级到 `message`，不会让 `silent_spawn` 失败。内部设计见 ADR 0003。

## 边界

- 目标仍能访问当前用户的文件、网络和注册表
- 部分 DirectX / Chromium / 受保护窗口会截出黑图，`all_black` 会标出来
- 不支持拖拽、组合键、OCR；`message` 模式下只有左键和窗口消息，轮询输入的引擎可能不吃（见「输入模式」）
- 工具成功只表示消息发出去了，界面变没变要再截一张图看

## 测试

```bash
npm test
```

`test:audio` 需要本机有活动的播放设备。
