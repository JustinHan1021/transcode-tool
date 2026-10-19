# 第三方组件与许可

本程序自身的源码是 [MIT](LICENSE)。但它**运行依赖 ffmpeg**，而打包好的下载包里内置了一份
ffmpeg 可执行文件，那份文件有它自己的许可。分开说清楚：

## 源码（MIT）

`trans/`、`转码工具.pyw`、`启动.bat`、`build_exe.py` 以及 `selftest/` 下的自检脚本，
全部以 MIT 许可发布，见 [LICENSE](LICENSE)。

## 内置的 ffmpeg（GPL v3）

`dist\转码工具\_internal\bin\` 下的 `ffmpeg.exe` 与 `ffprobe.exe` 来自：

| 项 | 值 |
| --- | --- |
| 项目 | FFmpeg — <https://ffmpeg.org/> |
| 版本 | 9.0.2 |
| 构建 | `9.0.2-essentials_build-www.gyan.dev`（<https://www.gyan.dev/ffmpeg/builds/>） |
| 许可 | **GNU General Public License v3**（构建参数含 `--enable-gpl --enable-version3`） |
| 完整许可文本 | [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt) |

这份构建启用了 `--enable-gpl`，并链接了 libx264、libx265、libxvid 等 GPL 组件，
因此整个 `ffmpeg.exe` 按 GPLv3 分发。它**没有**启用 `--enable-nonfree`，所以可以合法再分发。

### 对应源码在哪

GPLv3 要求分发二进制时提供对应源码。ffmpeg 的完整源码可从这里获得：

- 上游发行版：<https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz>
- 上游源码仓库：<https://git.ffmpeg.org/ffmpeg.git>（标签 `n9.0.2`）
- gyan.dev 公布的构建脚本与源码说明：<https://www.gyan.dev/ffmpeg/builds/#licensing>

如果你需要我提供本包中所含二进制对应的源码副本，请在这个仓库开一个 Issue，
我会把源码打包提供给你。这是 GPLv3 第 6(b) 条所要求的书面承诺，自本版本发布起三年内有效。

### 这对你意味着什么

- **用这个工具转码**：不受影响，输出文件归你。
- **改本程序的源码**：按 MIT 来，没有额外义务。
- **再分发整个下载包**（含内置 ffmpeg）：需要一起带上 GPLv3 全文和上面的源码获取途径。
  本仓库的 Release 包已经这样做了，直接转发整个压缩包即可。
- MIT 与 GPLv3 兼容，所以"MIT 代码 + GPLv3 二进制"打在一个包里分发是允许的；
  你自己的 MIT 源码不会因此被"传染"成 GPL。

## 其他

- 程序图标 `assets/app.ico` 为本项目自制。
- README 截图由 `selftest/shots.py` 从程序自身窗口抓取。
- 除此之外没有其它第三方运行时依赖：界面用 Python 自带的 tkinter，
  可选依赖 `tkinterdnd2` 只在你自己安装时生效，不随包分发。

## 换掉内置 ffmpeg

如果你希望分发一个不受 GPL 约束的版本，可以换成 **LGPL** 构建的 ffmpeg
（不带 `--enable-gpl`），把 `ffmpeg.exe` / `ffprobe.exe` 放进程序目录的 `bin\` 文件夹即可，
内置的那份不会被使用。代价是失去 x264 / x265 编码器——而那正是本程序主要的视频编码能力。
