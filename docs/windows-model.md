<!-- SPDX-License-Identifier: Apache-2.0 -->

# Windows 真实模型验收（实验阶段三）

本记录对应 [Windows 实验计划](../WINDOWS-EXPERIMENTAL-PLAN.md) 的阶段三。
使用现有本地 DeepSeek-V4.1-Flash-1017 Q8_0 全部十个 GGUF 分片，未下载、修改或复制模型权重。
单次生成、带历史的两轮对话、重复生成以及较长输出通过本机 OpenAI 兼容 HTTP API 验证。

## 模型与固定配置

| 项目 | 本次配置 |
| --- | --- |
| 系统 | Windows 11 x64，10.0.26200 |
| CPU | Intel Core Ultra 9 185H，22 个逻辑处理器 |
| 物理内存 | 33,945,935,872 字节，约 31.61 GiB |
| 模型 | `DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf` 至第十分片 |
| 文件总量 | 507,954,227,008 字节，约 473.07 GiB |
| 本地下载记录 revision | `1df54e7d1b1e442051ad393ce871870a929f243e` |
| 专家索引 | 120 个专家张量，384 个专家，46,080 条引用；专家载荷约 268.95 GiB |
| 存储与读取 | H 盘 NTFS；原 GGUF 引用索引，Windows IoRing 直接读取 |
| 计算后端 | CPU；`-t 12 -tb 12`，无 GPU |
| 上下文与批次 | `-c 1024 -b 64 -ub 16 -np 1` |
| 引擎内存设置 | `host.budget_mib = 4096`、`host.headroom_mib = 2048` |
| 预取 | `prefetch.depth = 2` |
| 启动设置 | `--defer-experts --fit off --no-warmup --cache-ram 0 --reasoning off` |
| 采样 | 请求级 temperature 0、seed 42；短请求最多 24 token，解码基线最多 64 token |
| 对话模板 | 仓库的 `deepseek-ai-DeepSeek-V4.1.jinja`，保留历史 `drop_thinking=false` |

`Q8_0` 是该发布的量化名称；索引识别的路由专家仍是原生 MXFP4。
下载 metadata 中的 revision/ETag、各分片大小及前 64 KiB 的 SHA-256 保存为 `model-files.json`。
这些记录固定本次本地文件身份，不等于重新计算、校验了全部约 473 GiB 权重的 SHA-256。

`host.budget_mib` 是引擎对进程私有内存的准入预算，驱动专家缓存回收；它不是 Windows 对总工作集的硬上限。
稠密权重和 Engram 表还通过文件映射访问，工作集会超过 4 GiB。
模型文件和专家载荷都超过整机物理 RAM，因此本次真实请求确实覆盖了超 RAM 模型。

## 本次结果

2026-11-03，`build-windows-cpu/model-validation/run-05/` 的完整五请求验收通过。
模型启动到健康接口可用为 3.063 秒；该时间包含上下文初始化的实际计算探测，且操作系统文件缓存未清空。
此前 `run-04/` 还完成了四个相同短请求；两次成功启动共完成九个请求。

| 请求 | 生成 token | 提示处理 / 秒 | 首个可见内容 / 秒 | 服务端解码 token/s | 请求总耗时 / 秒 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 单次 `WINDOWS_OK` | 5 | 6.933 | 11.062 | 1.212 | 11.064 |
| 对话第一轮 | 4 | 12.261 | 15.301 | 1.318 | 15.303 |
| 对话第二轮 `BLUE-731` | 5 | 10.499 | 14.636 | 1.214 | 14.637 |
| 重复 `WINDOWS_OK` | 5 | 6.726 | 10.810 | 1.226 | 10.812 |
| 64-token 解码基线 | 64 | 14.595 | 23.571 | 1.028 | 76.886 |

多轮第二次请求复用了 24 个缓存 token；短请求返回预期字符串。
64-token 基线说明 MoE 路由和专家，按设定 token 上限结束，输出未形成完整 80 词文章；其用途是解码计时。
该段返回了 55 个非空内容块，观察到了持续的 SSE 输出。

五请求运行的 261 个资源样本中，工作集峰值约 **10.26 GiB**，私有提交峰值约 **3.50 GiB**。
3,480 条 `ssd.io.read` 观测累计报告 **436.80 GiB** 专家需求读取、74,790 个读取请求、
5,354 次缓存命中和 24,930 次缓存未命中，读取失败为 **0**，观测层的 `pending_reads` 均为 **0**。
日志确认从七个含专家的 GGUF 分片直接读取，并使用 IoRing；另外三个分片仍由正常模型映射加载。
`cleanup.json` 确认本次服务已被脚本停止，资源监控无错误。

## 复现

沿用 [Windows CPU 构建](windows-cpu.md) 的 UCRT64 工具链。
验收脚本仅使用 Python 标准库，不需要安装包或新建虚拟环境。
从仓库根目录运行，按实际模型位置替换路径：

```powershell
$env:Path = 'C:\msys64\ucrt64\bin;' + $env:Path
cmake --build build-windows-cpu --parallel 8 --target inferssd-server

$env:PYTHONPATH = Join-Path $PWD 'framework\src'
python -X utf8 -m inferssd.source.gguf_alias `
  --model-glob 'H:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/*.gguf' `
  --out build-windows-cpu/model-validation/experts.tsv `
  --summary-json build-windows-cpu/model-validation/experts.json --repo .

python -X utf8 scripts/test-windows-model.py `
  --model 'H:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf' `
  --source-index build-windows-cpu/model-validation/experts.tsv `
  --output build-windows-cpu/model-validation/run-new
```

输出目录必须尚不存在，避免覆盖旧结果及遥测文件。索引只读取 GGUF 头部并引用权重范围，不另建专家包。
本机 46,080 条记录的载荷偏移不是 4096 字节对齐；运行时用对齐读取窗口及载荷偏移支持它们，
没有要求使用索引生成器的 `--require-aligned`。

脚本启动隐藏的原生服务端，使用临时回环端口，采样进程与系统资源，并顺序发送五个请求。
记录 SSE、响应、服务端计时、专家读取遥测及 `/metrics`，最后停止自己启动的服务。
失败也执行进程清理，并保存 `failure.json`；`cleanup.json` 中受控终止的 Windows 返回码 1 不表示模型请求失败。
如果执行沙箱以 Socket 10013 拦截回环请求，需要在允许本机回环 HTTP 的执行环境中运行同一命令。

## 验证与测量口径

单次请求和重复请求要求返回 `WINDOWS_OK`；多轮请求要求模型先记住 `BLUE-731`，再从完整对话历史中准确返回它。
较长输出要求 SSE 正常结束并有非空内容，其 token 上限用于建立短解码基线，不作为完整质量评测。
验收还检查实际设置、IoRing/直接读取记录、遥测序号连续、读取成功且已观测层无挂起读取。

`ttft_seconds` 是客户端从发出请求到收到首个非空内容或推理字段的时间；
`timings.prompt_ms` 和 `timings.predicted_per_second` 分别来自服务端的提示处理计时与解码计时。
短回复目前常被解析器合并成一个内容块，因此首个可见内容时间不能直接当作模型第一步解码的时间。
SSE 分块体验的进一步检查属于阶段四。

内存每 500 ms 采样一次，保存工作集、私有提交、页错误、系统可用物理内存和可用提交。
`ssd_demand_read_bytes` 汇总运行时 `ssd.io.read` 的读取字节，包含启动探测及所有请求；
这是专家读取工作量，不能当作整个磁盘的物理读取量或 SSD 吞吐基准，且不完整包含独立预取和映射页故障。
Windows `GetProcessIoCounters` 另存为进程 I/O 计数，口径与专家遥测不同。

每个运行目录保留：

- `launch.json`、`settings.ssd`、`model-files.json`：完整启动参数、预算、模型下载记录及哈希。
- `*.request.json`、`*.response.json`、`*.sse`：固定工作负载、原始 SSE 和计时。
- `native-events.jsonl`、`resources.jsonl`、`metrics.txt`：运行时遥测、资源样本和服务指标。
- `server.stdout.log`、`server.stderr.log`：原生服务日志。
- `summary.json`、`cleanup.json`：验收结果与本次进程的停止记录。

## 已知限制

默认的 `--fit on` 会先构造探测 context，提前占用独占创建的遥测路径，随后正式 context 报 `File exists`。
本阶段按固定配置使用 `--fit off`，没有修改生产代码来处理自动拟合；启用自动拟合加遥测仍是已知问题。
初次沙箱尝试的文件和失败记录保留在 `run-01` 至 `run-03`，`run-04` 完成四个短请求。

本次是小上下文、单并发和短输出的 CPU 技术原型验收。
不代表大上下文、长时间压力、其他量化、其他模型、GPU 或独立分发包已通过。
启动之前的操作系统文件缓存没有清空；启动时间不是严格的冷盘加载时间，
这里的速度也不能与 README 的 4K 提示、256 token、GPU 基准直接比较。
