# 图文语料来源与许可（第三方素材清单）

本目录的图片与文档**全部来自开源项目**，在此逐项登记来源、版本与许可。
收录标准：① 许可允许再分发（含 BSD / MIT / PSF 类）；② 体积可控（单文件 ≤ 512KB）；
③ 内容对"图文检索"有代表性（人像 / 动物 / 日常 / 天文 / 医学 / 纹理 / 文档 / 显微）。

**为什么必须登记**：这些文件进的是**仓库**，会被所有人看到并再分发。
没有出处与许可，等于把别人的作品"据为己有"——这是合规问题，不是形式问题。

## 图片（12 张）

来源包：**scikit-image 0.26.0**（PyPI wheel `scikit_image-0.26.0-*.whl`）内的 `skimage/data/`
许可：**BSD-3-Clause**（scikit-image 的 OSI 许可声明）

| 本地文件 | 原始文件 | 原始尺寸 | 处理 | 内容 |
|---|---|---|---|---|
| `images/astronaut.png` | `skimage/data/astronaut.png` | 512×512 | 等比（≤512） | 女宇航员官方肖像（NASA 公开照片） |
| `images/cat.png` | `skimage/data/chelsea.png` | 451×300 | 等比 | 虎斑猫面部特写 |
| `images/coffee.png` | `skimage/data/coffee.png` | 600×400 | 等比 → 512×341 | 杯碟与咖啡 |
| `images/rocket.jpg` | `skimage/data/rocket.jpg` | 640×427 | 等比 → 512×341 | 发射架上的运载火箭 |
| `images/hubble.jpg` | `skimage/data/hubble_deep_field.jpg` | 1000×872 | 等比 → 512×446 | 深空星系场 |
| `images/moon.png` | `skimage/data/moon.png` | 512×512 | 等比 | 月面灰度影像 |
| `images/brick.png` | `skimage/data/brick.png` | 512×512 | 等比 | 砖墙表面 |
| `images/grass.png` | `skimage/data/grass.png` | 512×512 | 等比 | 草地表面 |
| `images/retina.jpg` | `skimage/data/retina.jpg` | 1411×1411 | 等比 → 512×512 | 眼底彩色照片 |
| `images/camera.png` | `skimage/data/camera.png` | 512×512 | 等比 | 黑白：男子用三脚架相机拍摄 |
| `images/text.png` | `skimage/data/text.png` | 448×172 | 等比 | 手写文字灰度特写 |
| `images/cell.png` | `skimage/data/cell.png` | 550×660 | 等比 → 426×512 | 显微成像的圆形物体 |

处理说明：仅做**等比缩放**（最长边 ≤512）与色彩模式统一（RGB），**未做内容修改**。

## 文本（3 份 README）

来源：对应 PyPI **wheel 内 `*.dist-info/METADATA` 的 long description**（即项目官方 README 原文）。

| 本地文件 | 来源包 | 版本 | 许可 |
|---|---|---|---|
| `docs/pillow-README.md` | Pillow | 13.0.0 | MIT-CMU（Pillow License） |
| `docs/matplotlib-README.md` | matplotlib | 3.11.2 | PSF License（matplotlib License Agreement） |
| `docs/scikit-image-README.md` | scikit-image | 0.26.0 | BSD-3-Clause |

README 里的**远程图片链接**（徽章、logo）保持原样未下载 —— 它们不进语料，
只是正文里的外链（不影响检索口径）。

## 本目录自产文件

| 文件 | 说明 |
|---|---|
| `captions/img-*.txt`（12 份） | **本项目自写**的图注（四段式：主体 / 属性 / 场景 / 图中文字），**逐张看图人工核对**，不是从文件名猜的。对应生产里 `ImageIndex` 的 caption 块 |
| `图文混排.md` | 本项目自写：把图片与图注交错排布，用于验证「图文绑定 → 回显原图」 |

## 重新获取（可复现）

```bash
pip download --no-deps -d /tmp/pkgprobe scikit-image matplotlib pillow
# 图片：解包 wheel 的 skimage/data/ → 缩放到 ≤512 → 写入 images/
# 文本：解包 wheel 的 *.dist-info/METADATA → 取 long description → 写入 docs/
```
