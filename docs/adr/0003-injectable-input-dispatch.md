# 可替换的输入分发端口

Status: Accepted

私有桌面上没有真实输入队列可读：窗口消息能到达消息驱动的引擎，却到不了轮询输入的引擎（`GetAsyncKeyState` / DirectInput / RawInput / `GetCursorPos`，galgame 常见）。真实输入（`SendInput`）只作用于用户的输入桌面，既到不了私有桌面，也会打扰用户，双重不可行。

因此把「输入分发」定义成一个端口，两种实现按会话选择：

- **message**：现状，`window.py` 发有界窗口消息。默认。
- **inject**：往目标注入 payload DLL，让它的输入轮询改读一块共享内存的虚拟键状态表；后端只写内存，不关心 hook 怎么实现。

后端与 payload 之间只有一份契约（`pi_silent_input.h` 及其 Python 镜像 `inject_shm.py`）：512 字节映射，按 VK 码索引的键表（鼠标键同表）+ 光标 + seqlock。channel 名字经环境变量交给 payload；payload 装完 hook 后走命名管道回报一次握手，握手是「inject 生效」的唯一凭据。

注入时机：目标 `CREATE_SUSPENDED` 建好、`resume` **之后**再注入。刚挂起的进程连 kernel32 都还没映射（实测其模块表为空），远程线程要调的 `LoadLibraryW` 此刻并不存在；所以先 resume，等 loader 映射出 kernel32 再注入，hook 落在目标自身启动过程中、早于它读到有意义的输入。音频在 resume 前已 arm、窗口又在私有桌面，先 resume 既不漏音也不上屏。

## Considered Options

- 只发窗口消息：轮询引擎（大量 galgame）永远收不到输入。
- RDP 回环 / 虚拟机：换一整套隔离，重且偏离「私有桌面」这一现有骨架。
- 注入 + 共享内存端口：payload 由逆向侧按契约写，后端与实现解耦，可整体替换。

## Consequences

- `spawn`/`wait` 多报一个 `input_mode`（`inject` | `message`），让 AI 知道能否驱动轮询引擎、能否长按。
- `silent_key` 增加 `hold_ms`（长按跳过），仅 inject 会话有效；message 会话拒绝。
- payload DLL 路径可经 `silent_spawn` 的 `inject_dll32/64` 参数传入，或用 `PI_SILENT_GUI_INJECT_DLL32/64` 环境变量设默认（参数优先）；两者都未配置时退回 message，行为与从前一致。
- 注入失败（防篡改、加壳、目标未在时限内初始化、`LoadLibraryW` 为转发导出等）一律降级 message，绝不因此让 spawn 失败。
- 跨位数注入（64 位后端注入 32 位目标，galgame 常见）由 `inject.py` 统一处理：从目标模块表读它自己的 kernel32 基址，加上从磁盘对应位数 kernel32（`System32` / `SysWOW64`）解析出的 `LoadLibraryW` RVA，同位数与 64→32 走同一条路，不假设共享 ASLR 基址。转发导出等异常路径失败关闭而非半错注入。真机端到端（尤其 64→32 装载真实 DLL）待 payload 到位后验证。
