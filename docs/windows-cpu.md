<!-- SPDX-License-Identifier: Apache-2.0 -->

# Windows CPU 原生构建（实验阶段一）

2026-11-03 已在 Windows 11 x64（内核版本 10.0.26200）完成源码构建、16 项基础测试及无模型 HTTP 启动检查。
这是 [Windows 实验计划](../WINDOWS-EXPERIMENTAL-PLAN.md) 的阶段一结果。真实模型、完整 SSD 专家执行与性能验收仍需后续阶段完成。
阶段五的分发包、打包和干净环境验收见 [Windows 实验版分发包](windows-package.md)。

## 源码与依赖

本次基线提交为 `368df4f1a4d49c564ded47240b45fbd0dca8b084`，加上本次 Windows 构建修复。
构建脚本会记录实际 HEAD、本地修改 diff、相关源码 SHA-256、工具路径及版本，保存在构建目录中；不会切换或重置工作区。
复现这次结果时应保留这组修复，不能只检出基线提交。

使用 PowerShell 7、Git、MSYS2 **UCRT64** 工具链。已测版本：

| 组件 | 已测版本 |
| --- | --- |
| GCC / G++ | 16.1.0-5，目标 `x86_64-w64-mingw32`，POSIX 线程模型 |
| MinGW-w64 headers / CRT / winpthreads | `14.0.0.r179.g24aaa6147-1` |
| CMake | 4.3.4-1 |
| Ninja | 1.13.2-1 |

本机已有这些工具，未安装新的 Python 包或虚拟环境。本阶段没有 Python 依赖。
Windows API 头文件和导入库来自 MinGW-w64，包含 `ioringapi.h`；这条构建路径不要求额外安装 Visual Studio 或独立 Microsoft Windows SDK。
不要混用 MSYS `/usr/bin`、MINGW64（MSVCRT）或 UCRT64 的编译器及库。

## 重复构建与验收

在仓库根目录的 PowerShell 7 中运行，确保 MSYS2 的 `ucrt64/bin` 位于 PATH 前部。例如使用默认安装位置时：

```powershell
$env:Path = 'C:\msys64\ucrt64\bin;' + $env:Path
.\scripts\build-windows-cpu.ps1 -Jobs 8
```

脚本依次执行以下操作，任何失败都会终止验收：

1. 检查 x64 UCRT 编译器并记录源码和工具版本。
2. 通过 `windows-cpu` CMake preset 配置 Release CPU 构建。
3. 编译 `inferssd-server.exe` 及选定的基础测试。
4. 执行固定的 16 项 CTest，要求全部通过，禁止跳过或缺少测试。
5. 启动真实服务端的无模型路由模式，绑定 `127.0.0.1` 临时端口；验证 `/health`、`/v1/models` 和内嵌网页 `/`。
6. 自动停止本次启动的服务进程。

HTTP 检查直接访问回环地址，不使用系统代理。若在禁止回环连接的执行沙箱内出现 Socket 10013，应在允许本机回环连接的终端运行同一脚本；不需要更改 Windows 防火墙设置。

输出位于 `build-windows-cpu/`：

| 文件 | 用途 |
| --- | --- |
| `bin/inferssd-server.exe` | Windows x64 原生服务端 |
| `build-manifest.json`、`source.diff` | 基线版本、本地差异、源码哈希、构建工具与 SDK 包版本 |
| `configure.log`、`build.log` | 配置和编译日志 |
| `tests.log`、`windows-cpu-tests.xml` | 16 项测试结果与 JUnit 报告 |
| `server-version.log` | 可执行文件版本与编译器信息 |
| `server-smoke.json` | 健康接口、模型列表、网页状态及可执行文件 SHA-256 |
| `server.stdout.log`、`server.stderr.log` | 本次启动日志 |

也可分开配置与编译服务端：

```powershell
cmake --preset windows-cpu
cmake --build build-windows-cpu --parallel 8 --target inferssd-server
```

## 本次结果

- 服务端编译成功，`--version` 返回正常。
- 16/16 项基础测试通过，0 跳过。覆盖模型适配、运行策略、预填充成本、复制计划、内存预算、线程、内存、文件、读取环、源索引、测试资源、CPU 缓存策略、推理预算、服务端路由、Engram 和图开销。
- `/health` 返回 `{"status":"ok"}`，`/v1/models` 返回空的 OpenAI 格式列表，`/` 返回 HTTP 200 和 HTML。
- 仓库模块边界和布局检查通过。中文 Windows 的 Python 默认编码可能为 GBK，边界检查需使用 `python -X utf8 scripts/inferssd/check-layering.py`。

修复了三个本机构建或基础测试阻塞：

- MinGW POSIX 线程模型的 `std::thread::native_handle()` 是 `pthread_t`，需通过 `pthread_gethandle()` 转换，不能直接当作 Win32 `HANDLE`。
- MinGW 的 UCRT 头文件和默认 API-set 导入库未包含 `_set_thread_local_invalid_parameter_handler`；补上声明并链接 `ucrtbase`，保留线程局部的错误处理，避免改动进程全局处理器。
- Windows CPU Sets 是[软亲和性](https://learn.microsoft.com/en-us/windows/win32/procthread/cpu-sets)。本机对停放 CPU 的设置虽然返回成功，线程仍会在其他 CPU 上运行，导致现有线程测试失败。严格绑定接口现在同时设置并读回处理器组亲和性，CPU Sets 与其保持一致。

## 当前限制

- 只验收了这套 UCRT64 工具链；MSVC、Clang 和独立 Microsoft SDK 构建尚未验证。
- 使用 `GGML_NATIVE=ON`，产物针对当前 CPU；不能据此承诺在其他 CPU 上运行。
- 可执行文件仍依赖 UCRT64 的 `libgcc_s_seh-1.dll`、`libstdc++-6.dll` 和 `libwinpthread-1.dll`；运行时需保留相应 PATH。独立分发包属于阶段五。
- 本地 HTTP 与内嵌网页已启用，OpenSSL 和 HTTPS 模型下载支持暂时关闭。
- 保留了第三方代码的编译警告；这些警告没有阻止本次构建。
- 无模型路由模式仅用于启动验收。没有下载模型，没有验证聊天生成、完整 CPU 专家执行、缓存/预取压力、真实性能或全部 CTest；不能将基础测试通过视为阶段二、三完成。
