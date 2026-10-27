"""v2 文字来源与编码器：UIA 读字、PP-OCRv6 识别、ONNX 文本向量、空闲批次的 CLIP 图片向量。

各模块互不依赖，按需单独导入（UIA 需要 comtypes，OCR 需要 rapidocr，CLIP 需要 torch）。
"""
