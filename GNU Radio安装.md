# Windows 安装 GNU Radio

## 安装 Radioconda

1. 下载 Radioconda Windows x86-64 安装程序。本实验使用 `radioconda-2025.03.14-Windows-x86_64.exe`，其中的 GNU Radio 版本为 3.10.12.0。[Radioconda 发行页](https://github.com/radioconda/radioconda-installer/releases/tag/2025.03.14) · [GNU Radio Windows 安装说明](https://wiki.gnuradio.org/index.php/WindowsInstall)
2. 运行安装程序，选仅为当前用户安装，将安装目录设在一个空目录中。本机安装路径为 `D:\RadioLab\Radioconda`。
3. 打开 PowerShell，按下面的示例设置当前会话的环境。安装路径不同则修改 `$radio`。

```powershell
$radio = 'D:\RadioLab\Radioconda'
$env:Path = "$radio;$radio\Library\bin;$radio\Scripts;$env:Path"
& "$radio\python.exe" -c 'from gnuradio import gr; print(gr.version())'
& "$radio\python.exe" -c 'import numpy, scipy, matplotlib, osmosdr; print(numpy.__version__, scipy.__version__, matplotlib.__version__)'
```

本机输出版本为 GNU Radio `3.10.12.0`、NumPy `2.2.3`、SciPy `1.15.2`、Matplotlib `3.10.1`。`osmosdr` 导入成功。也可以从开始菜单启动 **GNU Radio Companion**。本仓库直接用 Python 构建 GNU Radio 流图。

## 验证实验程序

在仓库根目录运行以下命令，生成 DSB-SC 的图、WAV 和结果数据。

```powershell
& "$radio\python.exe" .\programs\modulation_demo.py --mode dsb-sc
```

如果提示找不到 `gnuradio`、`osmosdr` 或 DLL，先运行 `Get-Command python` 检查解释器路径，再确认 Radioconda 的 `Library\bin` 已加入当前窗口的 `PATH`。实验二还需要配置[电视棒驱动](电视棒驱动安装.md)和[录音工具](录音工具安装.md)。
