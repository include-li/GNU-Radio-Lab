# GNU Radio 通信实验

本仓库包含两个可复现的实验。实验一使用 GNU Radio 演示 2 MHz DSB-SC、2 MHz AM 和 100 MHz FM 的调制解调。实验二使用 SDR6 V4 接收北京 **FM 106.1 MHz 中国之声**，并与央广网同频道直播对齐。180 秒实测录音中，网络直播音频比 FM 解调音频晚 **32.36 秒**。网络端使用 FFmpeg 播放央广网官方 HLS 流。[央广网广播直播](https://www.cnr.cn/gbzb/) · [中国之声北京收听频率](https://www.cnr.cn/cbu_1/)

## 文档与结果

| 内容 | 入口 |
| --- | --- |
| 原始要求 | [实验题目](实验题目.md) |
| Windows 环境 | [GNU Radio 安装](GNU%20Radio安装.md) · [电视棒驱动安装](电视棒驱动安装.md) · [录音工具安装](录音工具安装.md) |
| 实验一设计与结果 | [调制解调设计](实验一-调制解调.md) · [DSB-SC 图](results/modulation/dsb-sc.png) · [AM 图](results/modulation/am.png) · [FM 图](results/modulation/fm.png) |
| 实验二设计与结果 | [FM 接收与延时设计](实验二-FM接收与延时.md) · [实收频谱](results/fm/rf-1061.png) · [时差图](results/fm/delay.png) · [逐段数据](results/fm/delay.json) |
| 原始证据 | [双路录音](results/fm/cnr-1061.wav) · [录音元数据](results/fm/cnr-1061.capture.json) · [原始 I/Q](results/fm/rtl-probe.cu8) · [离线验证](results/verification/offline-summary.json) |

## 快速运行

按安装文档准备 Radioconda 和 FFmpeg，在仓库根目录的 PowerShell 运行。将 `$radio` 改为本机的 Radioconda 安装目录。

```powershell
$radio = 'D:\RadioLab\Radioconda'
$env:Path = "$radio;$radio\Library\bin;$radio\Scripts;$env:Path"

# 演示三种调制和解调，弹出波形与频谱窗口
& "$radio\python.exe" .\programs\modulation_demo.py --show

# 用原始双路录音复算延时，无需连接电视棒
& "$radio\python.exe" .\programs\measure_delay.py results/fm/cnr-1061.wav --stereo --output output/fm/delay.json

# 连续执行七项离线检查
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_checks.ps1 -Prefix $radio -SkipRadio
```

接好天线和电视棒后，可以打开实时频谱并收听。关闭接收窗口后可进行双路录音，具体命令见[实验二](实验二-FM接收与延时.md)。

```powershell
& "$radio\python.exe" .\programs\fm_receiver.py --frequency 106.1
```

`programs/` 存放 Python 源程序，`results/` 存放提交的图片、音频、采样数据和验证记录。运行生成的文件默认写入 `output/`，批处理的逐项日志和退出码位于 `output/checks/`。
