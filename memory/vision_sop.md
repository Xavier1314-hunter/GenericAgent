# Vision API SOP

## ⚠️ 前置规则（必须遵守）

1. **先枚举窗口**：调用 vision 前必须先用 `pygetwindow` 枚举窗口标题，确认目标窗口存在且已激活到前台。窗口不存在就不要截图。
2. **🚫 禁止全屏截图**：必须先利用ljqCtrl截取窗口区域。能截局部就不截整窗口，能截窗口就绝不全屏。全屏截图在任何场景下都不允许。
3. **能不用 vision 就不用**：如果窗口标题/本地 OCR（`ocr_utils.py`）能获取所需信息，就不要调用 vision API，省 token 且更可靠。Vision 是最后手段。

## 快速用法

```python
import sys
sys.path.insert(0, '../memory')
from vision_api import ask_vision

result = ask_vision(image, prompt="详细描述这张图片的内容", backend="modelscope", timeout=120)
# image: 文件路径(str/Path) 或 PIL Image
# backend: 'modelscope'(默认) | 'openai' | 'claude'
# 返回 str：成功为模型回复，失败为 'Error: ...'
```

## 当前配置（已验证可用）

| 配置项 | 值 |
|--------|------|
| DEFAULT_BACKEND | `modelscope` |
| MODELSCOPE_API_KEY | `ms-69702d30-a0c8-4a49-a152-5bf45e291ace` |
| MODEL | `Qwen/Qwen3-VL-8B-Instruct` |
| API_BASE | `https://api-inference.modelscope.cn` |

**ModelScope 前置要求**（缺一不可）：
1. 阿里云账号绑定（https://modelscope.cn）
2. 实名认证（阿里云账号需完成 real-name verified）
3. 免费层仅 Qwen/Qwen3-VL-8B-Instruct 可用（235B 需付费）

## 如果没有 `vision_api.py`，初次构建vision能力

1. 复制 `memory/vision_api.template.py` → `memory/vision_api.py`
2. 头部的 MODELSCOPE_API_KEY 填入从 https://modelscope.cn/my/myaccesstoken 获取的 token
3. 直接测试：`ask_vision('图片路径', '描述', timeout=120)`
4. 若 401/403：检查账号是否绑定阿里云、是否完成实名认证
