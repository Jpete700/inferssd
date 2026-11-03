<!-- SPDX-License-Identifier: Apache-2.0 -->

# Windows 本地服务启动与使用

这是 [Windows 实验计划](../WINDOWS-EXPERIMENTAL-PLAN.md) 阶段四的原生 CPU 启动入口。
当前模型范围沿用 [阶段三验收](windows-model.md)：DeepSeek-V4.1-Flash-1017 Q8_0，路由专家保持 MXFP4。
阶段五的免安装分发包见 [Windows 实验版分发包](windows-package.md)。
启动器使用 Python 标准库和仓库已有的 GGUF 索引模块，无需安装 Python 包或新建虚拟环境。

## 启动

先按 [Windows CPU 构建说明](windows-cpu.md) 生成 `build-windows-cpu/bin/inferssd-server.exe`。
在仓库根目录的 PowerShell 7 中运行，模型参数指向第一分片：

```powershell
.\scripts\windows-server.ps1 start `
  --model 'H:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf' `
  --memory-mib 4096 --context 1024 --port 8080
```

看到 `"status": "ready"` 后打开 <http://127.0.0.1:8080> 聊天。
也可以添加 `--open-browser`，就绪后由启动器打开浏览器。
`start` 在后台保留一个监控进程和一个原生服务端；命令返回后服务继续运行。

配置项：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--model` | 必填 | 本地第一 GGUF 分片；其余分片须同目录且完整 |
| `--memory-mib` | `4096` | 引擎私有内存准入预算，MiB |
| `--context` | `1024` | 单个会话的上下文 token 数 |
| `--port` | `8080` | 本机 HTTP 端口 |
| `--threads` | 最多 12 | CPU 推理与提示处理线程数 |
| `--timeout` | `300` | 模型启动健康检查超时，秒 |
| `--binary` | 构建目录中的服务端 | 自行选择原生可执行文件 |
| `--runtime-dir` | 常见的 UCRT64 安装目录 | MinGW 运行时 DLL 所在目录；原有 PATH 也保留 |
| `--state-dir` | `build-windows-cpu/windows-server` | 状态与各次运行日志目录 |

4 GiB 预算约束的是引擎对进程私有内存的准入，不能理解为进程总工作集的 Windows 硬上限。
映射的稠密权重和 Engram 表另占工作集。CPU、上下文与预算会共同影响运行占用。
默认使用 `--fit off` 避开阶段三发现的自动拟合与独占遥测文件冲突。

启动前检查模型路径、首分片编号、完整分片集、GGUF 架构、专家格式、端口以及服务端/DLL 是否可运行。
`doctor` 子命令可在不加载模型时检查 Python、服务端、DLL、框架模块、模板、状态目录和端口；加上 `--model` 还会验证分片集与 `deepseek41` 架构：

```powershell
.\scripts\windows-server.ps1 doctor --model 'H:/Models/DeepSeek-V4.1-Flash-1017/Q8_0/DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf'
```

启动器每次从 GGUF 头部生成新的专家引用索引，不复制或改写权重，不需要另外准备专家包。
当前只允许已验证的 `deepseek41` 模型架构及 MXFP4 路由专家。

## 状态、日志和停止

```powershell
.\scripts\windows-server.ps1 status --port 8080
.\scripts\windows-server.ps1 logs --port 8080 --lines 40
.\scripts\windows-server.ps1 stop --port 8080
```

若启动时指定了 `--state-dir`，查询和停止时也传入相同目录。
`status` 在就绪时返回退出码 0，未就绪/停止时返回 2。
`stop` 可重复执行；它通过本次运行的停止请求让监控进程退出，不按进程名称批量终止程序。

每次启动独立保存 `config.json`、`preflight.json`、`launch.json`、`settings.ssd`、专家索引、
`native-events.jsonl`、`server.stdout.log`、`server.stderr.log`、`supervisor.log` 及最终 `result.json`。
启动失败的终端提示包含原因和日志目录。
正常停止以 Windows 进程终止方式结束原生服务，`server_exit_code` 可以为 1；运行状态 `stopped` 表示受控停止。

需要前台使用并通过 Ctrl+C 结束时，把 `start` 替换为 `serve`；浏览器自行打开。
启动尚未就绪时，也能从另一终端执行 `stop` 取消启动。

监控进程将服务端加入 Windows Job Object，并保持唯一的 Job 句柄。
监控进程异常退出或被强制终止时，Windows 自动回收该 Job 中的服务端进程。
进程身份同时检查 PID 和创建时间；同端口的启动器通过文件锁互斥，历史停止文件用运行 token 隔离。

## API、流式输出和取消

OpenAI 兼容客户端的基础地址为 `http://127.0.0.1:8080/v1`，模型名为 `windows-deepseek`。
无需 API key。服务只绑定 `127.0.0.1`，没有开放局域网监听。

PowerShell 请求示例：

```powershell
$request = @{
  model = 'windows-deepseek'
  messages = @(@{ role = 'user'; content = '请简单介绍自己。' })
  max_tokens = 64
  temperature = 0
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8080/v1/chat/completions `
  -Method Post -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($request))
```

请求设置 `stream: true` 时返回 SSE，正常结束带 `data: [DONE]`。
API 客户端关闭正在生成的 HTTP 连接可取消请求；浏览器点击停止使用相同的连接中止机制。
停止生成后，可在同一服务上继续下一次请求。

自动 API 验收会发送真实生成请求，请在无人使用服务时运行：

```powershell
python -X utf8 scripts/test-windows-service.py `
  --url http://127.0.0.1:8080 `
  --output build-windows-cpu/service-validation/api-new
```

它检查网页响应、模型列表、错误请求、非流式回复、跨多个时刻的 SSE 内容、
中途断开后的槽位释放，以及取消后的新请求恢复；输出目录必须尚不存在。

进程清理测试不需要模型：

```powershell
python -X utf8 tests/integration/test-windows-server.py -v
```

## 本机验收记录

进程所有权的四项测试通过：Job 关闭回收子进程、监控异常退出回收服务、
创建时间不符时拒绝操作 PID、忽略旧运行的停止请求。
模型缺失、首分片错误、端口占用、可执行文件缺失、子进程启动失败、重复启动及准备阶段停止已实测。
后台启动命令返回后，健康接口、模型列表和内嵌网页仍能正常访问。

浏览器验收由用户手动操作，已确认正常回复、上下文记忆、持续输出、点击停止后继续聊天以及刷新重连均通过。

2026-11-03，本机 `build-windows-cpu/service-validation/api-02/summary.json` 记录自动 API 验收通过：

| 检查 | 结果 |
| --- | --- |
| 健康接口、模型列表、内嵌网页 | 正常响应 |
| 无效聊天请求 | 返回 HTTP 400 |
| 非流式生成 | 返回 `WINDOWS_API_OK` |
| 流式生成 | 23 个非空内容块，跨约 21.59 秒到达，正常收到 `[DONE]` |
| 中途取消 | 收到 2 个内容块时断开 HTTP，槽位约 2.18 秒内释放 |
| 取消后恢复 | 新请求返回 `AFTER_CANCEL_OK` |

正常停止、重复停止、端口释放、含空格状态目录的重启均通过。
另一次真实模型运行中主动结束其服务进程，监控进程记录 `failed` 后退出；随后再次启动和停止成功。
本次所有测试服务最终均已停止。失败检查与进程测试日志保存在同一 `service-validation/` 目录。
验证使用上一阶段的 4 GiB 预算、1024 上下文及 CPU 配置。

## 边界

这是仓库内的 Windows CPU 启动流程；独立分发包、无开发工具链机器复测属于阶段五，GPU 加速属于阶段六。
Windows 完整 Python 受管调优框架仍未接入。
执行沙箱若返回 Socket 10013，需允许本机回环 HTTP 后运行相同命令。
修改模型、量化、预算或大幅提高上下文长度后，需要重新验收相应配置。
