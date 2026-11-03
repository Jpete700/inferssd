<!-- SPDX-License-Identifier: Apache-2.0 -->

# Windows 实验版分发包（阶段五）

这是 [Windows 实验计划](../WINDOWS-EXPERIMENTAL-PLAN.md) 阶段五的结果：一个解压后即可运行的 Windows x64 CPU 实验包。
包内含原生服务端、MinGW 运行时 DLL、启动器、便携 Python、聊天模板、示例配置和校验清单；模型权重不包含在包内。

> 实验版只验证了本页列出的硬件、模型和固定配置，不代表其他 Windows 机器、模型或量化已通过。

包内 `docs/windows-cpu.md`、`windows-model.md` 和 `windows-server.md` 是源码仓库阶段的验收记录，其中的构建命令和验收脚本路径只在源码检出中有效；分发包用户请以包根 `README.md` 和本页为准。

## 包内容

| 路径 | 用途 |
| --- | --- |
| `bin/inferssd-server.exe` | Windows x64 原生服务端 |
| `bin/libgcc_s_seh-1.dll`、`libstdc++-6.dll`、`libwinpthread-1.dll` | UCRT64 运行时，放在可执行文件旁 |
| `scripts/windows-server.ps1` | 启动入口，自动优先使用包内 Python |
| `scripts/windows-server.py` | `start` / `serve` / `status` / `logs` / `stop` / `doctor` |
| `python/` | 裁剪后的 CPython 便携运行时，无需系统安装 Python |
| `framework/src/inferssd/` | 启动器生成 GGUF 专家索引所需的只读模块 |
| `engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja` | 已验证的对话模板 |
| `examples/start-deepseek.ps1` | 修改模型路径后可直接运行的示例 |
| `docs/` | Windows 构建、模型验收、启动器说明 |
| `inferssd-package.json` | 版本、Git 修订、Python 版本、服务端哈希 |
| `checksums.txt` | 包内每个文件的 SHA-256 |

包约 21 MiB（zip），解压后约 56 MiB（不含模型）。模型文件单独准备，放在 SSD 或任意本地磁盘上。

## 使用

1. 把 zip 解压到可写目录（不要直接在 zip 内运行）。
2. 准备模型：DeepSeek-V4.1-Flash-1017 Q8_0 的全部十个 GGUF 分片放在同一目录，路径指向第一分片 `...-00001-of-00010.gguf`。
3. 自检：

```powershell
.\scripts\windows-server.ps1 doctor
```

`doctor` 会检查 Python、服务端、DLL、框架模块、模板、状态目录和端口；加上 `--model <第一分片>` 还会检查分片集和 `deepseek41` 架构。

4. 启动：

```powershell
.\scripts\windows-server.ps1 start `
  --model 'D:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf' `
  --memory-mib 4096 --context 1024 --port 8080 --open-browser
```

看到 `"status": "ready"` 后打开 <http://127.0.0.1:8080>。
也可以直接运行 `examples\start-deepseek.ps1`（先修改其中的模型路径）。

5. 状态、日志和停止：

```powershell
.\scripts\windows-server.ps1 status --port 8080
.\scripts\windows-server.ps1 logs --port 8080 --lines 40
.\scripts\windows-server.ps1 stop --port 8080
```

OpenAI 兼容客户端地址为 `http://127.0.0.1:8080/v1`，模型名为 `windows-deepseek`，无需 API key。
服务只绑定 `127.0.0.1`。

默认状态下，分发包把运行记录写在 `%LOCALAPPDATA%\InferSSD\windows-server`；源码仓库内仍写在 `build-windows-cpu\windows-server`。
可用 `--state-dir` 覆盖。

## 已测配置与结果

2026-11-03 的本机验收使用以下配置；分发包沿用同一二进制、启动参数和模型范围。

| 项目 | 配置 |
| --- | --- |
| 系统 | Windows 11 x64，10.0.26200 |
| CPU | Intel Core Ultra 9 185H，22 个逻辑处理器 |
| 物理内存 | 33,945,935,872 字节，约 31.61 GiB |
| 模型 | DeepSeek-V4.1-Flash-1017 Q8_0，十个分片，约 473.07 GiB |
| 专家 | 120 个专家张量，384 个专家，46,080 条引用，载荷约 268.95 GiB |
| 存储/读取 | H 盘 NTFS，Windows IoRing 直接读取 |
| 计算 | CPU，`-t 12 -tb 12`，无 GPU |
| 上下文/批次 | `-c 1024 -b 64 -ub 16 -np 1` |
| 引擎预算 | `host.budget_mib = 4096`、`host.headroom_mib = 2048`，预取深度 2 |
| 启动设置 | `--defer-experts --fit off --no-warmup --cache-ram 0 --reasoning off` |

真实模型结果：

| 指标 | 结果 |
| --- | --- |
| 单次 `WINDOWS_OK` | 5 token，首个可见内容 11.06 s，解码 1.212 token/s |
| 两轮对话 `BLUE-731` | 首轮 15.30 s，次轮复用 24 个缓存 token，14.64 s |
| 64-token 解码基线 | 1.028 token/s，请求总耗时 76.89 s |
| 资源 | 工作集峰值约 10.26 GiB，私有提交峰值约 3.50 GiB |
| 专家读取 | 3,480 条遥测，436.80 GiB 需求读取，74,790 次请求，0 失败 |
| API 流式 | 23 个非空 SSE 内容块，正常 `[DONE]` |
| 取消 | 断开后约 2.18 s 释放槽位，后续请求返回 `AFTER_CANCEL_OK` |

详细记录见 [真实模型验收](windows-model.md) 和 [本地服务说明](windows-server.md)。
这些数字来自 CPU 实验配置，不能与 README 中 GPU、4K 提示、256 token 的基准直接比较。

2026-11-04 对最终 zip 又做了一次分发包验收：`verify-windows-package.ps1` 的 9 项检查全部通过；包内启动器在收窄 PATH 下用 `doctor --model` 识别 10 个分片、`deepseek41` 架构和 507,954,227,008 字节；随后通过包内 `windows-server.ps1 start` 启动真实模型并发送一次请求，返回 `WINDOWS_PACKAGE_OK`（8 个 completion token），`stop` 后服务进程无残留。

## 从源码生成包

先按 [Windows CPU 构建](windows-cpu.md) 生成 `build-windows-cpu/bin/inferssd-server.exe`，然后在仓库根目录运行：

```powershell
.\scripts\package-windows-cpu.ps1 -Version 0.0.0-windows-experimental
```

脚本会：

1. 检查服务端和 UCRT64 运行时 DLL，并按可执行文件的导入表收集 MinGW DLL。
2. 复制启动器、框架只读模块、对话模板、示例和文档。
3. 从当前 Python 安装生成裁剪后的便携运行时（排除 `site-packages`、`test`、IDLE、Tk 等）。
4. 写入 `inferssd-package.json` 和全文件 `checksums.txt`。
5. 生成 `dist/inferssd-windows-x64-cpu-<版本>.zip` 和同名 `.sha256`。

可选参数：

| 参数 | 含义 |
| --- | --- |
| `-Build` | 已有构建目录，默认 `build-windows-cpu` |
| `-Output` | 输出目录，默认 `dist` |
| `-PythonRuntime` | 指定完整 CPython 安装目录；默认取 `python` 命令的真实安装位置 |
| `-RuntimeDir` | 指定 MinGW DLL 目录；默认取 `gcc` 所在目录 |
| `-NoZip` | 只生成解压目录，不生成 zip |
| `-Force` | 覆盖同版本输出 |

生成后可以在没有开发工具链 PATH 的环境中复验：

```powershell
.\scripts\verify-windows-package.ps1 -PackageZip .\dist\inferssd-windows-x64-cpu-0.0.0-windows-experimental.zip
```

验收脚本解压到临时目录，把 PATH 收窄为 `System32`，并清除 `PYTHONHOME` / `PYTHONPATH`，然后检查：

- `checksums.txt` 中每个文件；
- 包内没有 `site-packages`、`__pycache__`、构建日志等开发产物；
- `bin\inferssd-server.exe --version` 能在没有 MSYS2 的环境启动；
- 无模型路由模式下的 `/health`、`/v1/models` 和内嵌网页；
- 包内 Python 可导入 `ssl`、`ctypes`、`csv`；
- 启动器 `doctor` 的全部检查；
- 包内 `windows-server.ps1` 通过 Windows PowerShell 5.1 调用 `status` 并返回预期退出码 2。

结果写到 `dist/verification-<版本>.json`。这次本机验收使用的是同一台机器上的干净 PATH 模拟，不是第二台未安装开发工具的物理机；这一点仍是阶段五的已知边界。

## 自动构建与回归

`.github/workflows/ci.yml` 新增了 `windows-cpu` 作业：在 `windows-latest` 上用 MSYS2 UCRT64 编译服务端和 16 项基础测试，执行无模型 HTTP 冒烟，生成包，再用收窄 PATH 的验收脚本复验，并运行 `tests/integration/test-windows-package.py` 检查 zip 布局与校验和。
`release.yml` 在版本标签发布后构建同样的包并附加到 GitHub Release。

## 已知限制

- 只验证了 Windows 11 x64 + UCRT64 构建；MSVC、Clang、ARM64 未验证。
- 只接受 `deepseek41` 架构和 MXFP4 路由专家；其他模型或量化会被启动前检查拒绝。
- 只验证了 DeepSeek-V4.1-Flash-1017 Q8_0；Muse 等其他模型不在本包范围内。
- `GGML_NATIVE=ON`，二进制针对构建机器 CPU；不能承诺在其他 CPU 上获得相同性能或可运行。
- 默认 `--fit off`；自动拟合与独占遥测文件的冲突仍未修复（见 [真实模型验收](windows-model.md)）。
- 小上下文、单并发、短输出是本次验收范围；长上下文、长时间压力、多并发未验证。
- CPU 模式；Vulkan/CUDA 稠密层加速属于阶段六。
- 本地 HTTP，没有 HTTPS 和模型下载；模型需手动准备。
- 服务只绑定 `127.0.0.1`，不开放局域网。
- 包未做代码签名，Windows SmartScreen 可能提示；请用 `checksums.txt` 核对内容。
- 独立分发包仍依赖 Windows 10/11 自带的 UCRT（`ucrtbase.dll`）；包内只带 MinGW 的三个运行时 DLL。
- 干净环境验收是同机 PATH 收窄模拟；尚未在第二台无开发工具的物理机上复测。
