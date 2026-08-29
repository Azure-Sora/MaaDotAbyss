"""M0-3 MiMo-V2.5 视觉调用测试。

用法: python poc/m0_mimo_test.py [截图路径]   # 缺省用前台截图
"""
import base64
import sys
from pathlib import Path

from openai import OpenAI

from common import OUT, ROOT

BASE_URL = "https://api.xiaomimimo.com/v1"
MODEL = "mimo-v2.5"

PROMPT = (
    "这是一张游戏画面截图。请用中文回答：\n"
    "1) 当前处于什么界面/状态？\n"
    "2) 列出画面中的可交互元素（按钮、入口、图标等）及各自在画面中的大致位置（左上/中央/右下等）。\n"
    "3) 界面文字是什么语言？"
)


def call(img_path: Path):
    key = (ROOT / ".local" / "mimokey.txt").read_text(encoding="utf-8-sig").strip()
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    client = OpenAI(api_key=key, base_url=BASE_URL)
    kwargs = dict(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
    )
    try:
        # MiMo-V2.5 是推理模型：reasoning 会消耗 completion 预算，1024 只够思考、正文为空
        return client.chat.completions.create(max_completion_tokens=4096, **kwargs)
    except Exception as e:  # 部分 OpenAI 兼容端点只认 max_tokens
        print(f"[fallback] max_completion_tokens 失败（{e.__class__.__name__}），改用 max_tokens 重试")
        return client.chat.completions.create(max_tokens=4096, **kwargs)


def main():
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / "fg_screencap.png"
    if not img.exists():
        raise SystemExit(f"截图不存在: {img}（先跑 m0_device_test.py）")
    resp = call(img)
    print(resp.choices[0].message.content)
    print("---")
    print("usage:", resp.usage)


if __name__ == "__main__":
    main()
