# pi-silent-gui

Windows 上给 Pi 用的窄 GUI 冒烟工具：在私有桌面里启动程序，等到窗口，截图，点击，输入，按键，然后清掉。

## Language

**Session**:
一次私有桌面 GUI 运行。调用方只通过 `session_id` 引用它。某一刻可读：是否仍有活进程、启动 exe 的退出码、当前全部顶层 Window。
_Avoid_: Job, desktop, broker, capability, runtime

**Window**:
Session 里一个可见的顶层窗口。截图和点击共用它的窗口矩形，原点在左上角。
_Avoid_: HWND 身份、coordinate_space、dispatch metadata

**Capture**:
该窗口的一张原始尺寸 PNG。工具结果同时给出磁盘路径和同一份像素的 image 附件。
_Avoid_: 证据包、基线比对、缩放预览、all_black 之外的取证字段

**Click**:
按 Capture 同一坐标系里的整数像素点左键。同一点可用 `count` 连点。
_Avoid_: 元素寻址、UIA pattern、SendInput、多点脚本

**Type**:
向当前焦点控件写入一段文本。
_Avoid_: 剪贴板、组合键、IME 协议

**Key**:
按下一个有名字的键，例如 `return` 或 `tab`。同一键可用 `count` 连按。
_Avoid_: 任意 vk 仪式、修饰键保持

**Wait**:
观察 Session，直到出现匹配的 Window、Session 结束、或超时。成功和失败都带同一份快照。
_Avoid_: 元素级 actionability、断言谓词
