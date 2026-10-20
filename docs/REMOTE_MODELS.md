# 在自己的 Linux GPU 服务器部署

先确认自己可用的 GPU、显存与磁盘，不停止别人的进程。以下模型通过 Transformers 运行；本机不加载权重。

## 准备文件与环境

在自己的 SSH 配置中定义 `model-server` 别名，或在启动命令中传入自己的别名。不要把私人主机配置提交到仓库。

```bash
ssh model-server 'mkdir -p ~/classroom-models'
scp scripts/qwen_asr_service.py scripts/qwen_event_service.py scripts/download_qwen_models.py scripts/start_model_services.sh model-server:classroom-models/
ssh model-server
cd ~/classroom-models
python3.11 -m venv .venv
.venv/bin/python -m pip install torch==2.10.0 qwen-asr==0.0.6 transformers==4.57.6 accelerate==1.12.0 fastapi uvicorn python-multipart modelscope
.venv/bin/python download_qwen_models.py --with-events
```

下载来自官方 Qwen 仓库。GPU 驱动应能运行相应 PyTorch CUDA 构建；若安装环境不同，请按 PyTorch 官方安装说明选择兼容版本。

如果已有文字模型接口，可省略 `--with-events`，只下载约 6.54GB 的转写和对齐权重。

## 启动自己的服务

```bash
# 选择自己可用的一张卡；服务只绑定服务器回环地址。
YANHE_GPU=0 YANHE_SERVICES='asr event' bash start_model_services.sh
```

脚本会生成专用 `service.key`，保存各服务进程编号和日志，不调整其他服务。ASR 接口为 `127.0.0.1:18765/v1/audio/transcriptions`，文字接口为 `127.0.0.1:18766/v1/chat/completions`。

ASR 要求中文、单声道 16kHz WAV、每块最多 330 秒，返回真实对齐时间戳。工作台默认约 300 秒核心分块并附重叠上下文。对齐得到的零时长字符只与相邻原始区间取包络，不编造时间；整段没有有效正时长时拒绝结果。

两个模型服务与真实课堂音频均应留在自己的环境中；不要将模型接口直接绑定公网。

## Windows 工作台通过 SSH 接入

确保已经安装本机工作台依赖和 PowerShell 7，然后：

```powershell
pwsh -NoProfile -File scripts/start_remote_workbench.ps1 -SshHost model-server
```

它将服务器两个端口映射到本机回环地址，并注入默认模型设置。令牌副本位于忽略目录 `runs/remote/`。

如果复用服务器已有的文字服务，可明确指定它的实际端口及模型名称，例如：

```powershell
pwsh -NoProfile -File scripts/start_remote_workbench.ps1 -SshHost model-server -EventRemotePort 8000 -EventModel qwen3-4b
```

前提是该接口的访问策略允许你使用；现有服务如需要独立密钥，请在工作台模型设置中配置自己的凭据。

Qwen 的思考模式较慢，通常更适合区分授课内容和真正通知。界面可以切换模式。输出必须通过格式与原文引用校验；失败时保留部分状态而不假装“未检出”。

## Linux / macOS 工作台

也可以手动建立 SSH 本地转发，在“模型设置”填写本机回环地址与相应模型名。自建 ASR 的专用令牌作为 ASR Key；文字服务的 Key 按其实际访问策略填写。

这只是可选部署方案。模型权重、服务令牌和服务器配置均不随源码发布。
