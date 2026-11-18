"""格式专项测试（doc/testing/01-文件格式解析测试矩阵.md 的落地目录）。

为什么单开一个目录：格式轴有**自己的**判定口径（8 条通用解析契约 + 扩展契约），
和"接口是否可用"是两件事。混进 tests/integration 会被后端层的断言风格稀释，
格式回归（比如表格被切碎、warning 丢失）就再也看不出来。

- `fixtures.py`：现场造件工厂（**不落二进制样本**）
- `contract.py`：通用解析契约断言器（C1~C8 + 扩展契约 C9~C12）
- `test_all_extensions.py`：**全部 52 个扩展名**逐一过契约
- `test_pdf_matrix.py`：PDF 专项（30 例 / 13 类样本）
- `test_other_formats.py`：DOCX / XLSX / PPTX / MD / TXT / HTML / CODE / IMG / AUD
- `test_xformat.py`：跨格式一致性（XFM-01~10）
"""
