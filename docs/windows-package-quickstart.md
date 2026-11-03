<!-- SPDX-License-Identifier: Apache-2.0 -->

# InferSSD Windows x64 CPU 实验版

这是 InferSSD 的 Windows 原生 CPU 实验包：通过 SSD 按需读取 MoE 专家，CPU 计算，提供浏览器聊天和本地 OpenAI 兼容 API。
包内含服务端、MinGW DLL、启动器和便携 Python；模型权重需要单独准备。

> 只验证了 DeepSeek-V4.1-Flash-1017 Q8_0、1024 上下文和本页列出的硬件。实验版，不是稳定发行版。

## 快速开始

1. 解压到可写目录。
2. 准备模型的十个 GGUF 分片，路径指向第一分片。
3. 自检：

```powershell
.\scripts\windows-server.ps1 doctor --model 'D:\Models\DeepSeek-V4.1-Flash-1017\Q8_0\DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf'
```

4. 启动：

```powershell
.\scripts\windows-server.ps1 start `
  --model 'D:\Models\DeepSeek-V4.1-Flash-1017\Q8_0\DeepSeek-V4.1-Flash-1017-Q8_0-00001-of-00010.gguf' `
  --memory-mib 4096 --context 1024 --port 8080 --open-browser
```

5. 打开 <http://127.0.0.1:8080> 聊天；API 地址为 `http://127.0.0.1:8080/v1`，模型名 `windows-deepseek`，无需 API key。

```powershell
.\scripts\windows-server.ps1 status --port 8080
.\scripts\windows-server.ps1 logs --port 8080 --lines 40
.\scripts\windows-server.ps1 stop --port 8080
```

也可以先修改 `examples\start-deepseek.ps1` 里的模型路径，再直接运行。

## 已测配置

| 项目 | 配置 |
| --- | --- |
| 系统 | Windows 11 x64，10.0.26200 |
| CPU / 内存 | Intel Core Ultra 9 185H，22 逻辑处理器 / 约 31.61 GiB |
| 模型 | DeepSeek-V4.1-Flash-1017 Q8_0，10 个分片，约 473.07 GiB |
| 配置 | 4 GiB 引擎预算、1024 上下文、CPU 12 线程、无 GPU |
| 速度 | 64-token 基线约 1.03 token/s；首个可见内容约 10.8–15.3 s |
| 内存 | 工作集峰值约 10.26 GiB；专家读取 436.80 GiB，0 失败 |

详细数据和限制见 `docs/windows-package.md`、`docs/windows-model.md` 和 `docs/windows-server.md`。

## 边界

- 只接受 `deepseek41` 架构和 MXFP4 路由专家。
- 二进制针对构建机器 CPU（`GGML_NATIVE=ON`），其他 CPU 未验证。
- 小上下文、单并发；长上下文和多并发未验证。
- CPU 模式；GPU 加速未包含。
- 模型不随包分发；包未签名，请用 `checksums.txt` 核对。
- 运行记录默认写到 `%LOCALAPPDATA%\InferSSD\windows-server`。
