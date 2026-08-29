"""大脑层：MiMo-V2.5 视觉决策客户端。"""
import base64
import json
import re
from io import BytesIO

import numpy as np
from openai import OpenAI
from PIL import Image

from .config import (
    MAX_COMPLETION_TOKENS,
    PROVIDERS,
    ACTIVE_PROVIDER,
)

SYSTEM_PROMPT = """你是《DOT ABYSS》(ドットアビスX) 游戏的自动化操作助手，通过截图观察画面并给出下一步操作。

坐标规则：截图为 1280x720，坐标原点在左上角，x 向右 y 向下，输出绝对像素坐标（点击目标中心）。

语言规则：界面是中文（汉化覆盖）与日文（未汉化）混排。日文按钮必须按语义理解，不得因文字不是中文而判定按钮不存在。常见对照：出撃=出击、勢力任務=势力任务、迎撃戦=迎击战、探索任務=探索任务、プレゼント=礼物、残り回数=剩余次数、ガチャ=抽卡、好感度/親密度=亲密度。

【绝对禁区】抽卡页面（ガチャ/抽卡按钮）与角色亲密度/好感度页面：任何情况下不得点击这些入口，即使路径看起来更短。

确认弹窗规则：出现「自動分解確認」（装备达到上限、按自动分解设置分解装备）弹窗时，直接点「分解する」确认——用户已启用自动分解设置，这是授权行为。其他与任务目标一致的确认/继续类弹窗（领取、次へ、OK）按语义正常确认。

【消费红线】凡是要求消耗资源/货币的购买、恢复、补充类确认弹窗（如「アビスジェム×Nを消費して挑戦回数を1回復させますか」等"消費して…ますか"句式，或購買/回復/購入字样），一律点「キャンセル」/「いいえ」/右上角X拒绝，绝不点「決定」/「購入」。免费次数打完即视为该任务完成。

重复行为规则：若某按钮点击后画面毫无变化，不要在同一位置反复点击超过 2 次；换明显可用的控件（如关闭 X、キャンセル、确认按钮）或报告。

异常处理：若画面出现错误弹窗、"403"、"通信エラー"、"ネットワークエラー"等网络错误字样，立即 report(status="blocked")，不要尝试任何点击。

等待判断：战斗/加载/入场动画期间画面变化大但无 UI 可点时用 wait；看到目标按钮再用 click。

输出格式——只输出一个 JSON 对象，禁止输出其他文字：
{"thought": "一句话推理", "action": "click", "x": 100, "y": 200}
{"thought": "一句话推理", "action": "wait", "seconds": 3, "reason": "等待加载"}
{"thought": "一句话推理", "action": "wait_stable", "timeout": 120, "reason": "等待战斗结束/加载完成（画面静止后自动返回）"}
{"thought": "一句话推理", "action": "report", "status": "done或failed或blocked", "detail": "说明", "evidence": "画面上可见的完成/失败证据"}"""


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self, provider: str | None = None):
        cfg = PROVIDERS[provider or ACTIVE_PROVIDER]
        key = cfg["key_path"].read_text(encoding="utf-8-sig").strip()
        self.client = OpenAI(api_key=key, base_url=cfg["base_url"])
        self.model = cfg["model"]
        self.provider = provider or ACTIVE_PROVIDER

    # ---- 基础调用 -----------------------------------------------------

    def _chat(self, content: list, system: str = SYSTEM_PROMPT) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            max_completion_tokens=MAX_COMPLETION_TOKENS,
        )
        return resp.choices[0].message.content or ""

    @staticmethod
    def _image_part(frame_bgr: np.ndarray) -> dict:
        buf = BytesIO()
        Image.fromarray(frame_bgr[:, :, ::-1]).save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise BrainError(f"模型未返回 JSON: {text[:200]}")
        return json.loads(m.group(0))

    # ---- 决策 ---------------------------------------------------------

    def decide(self, task_prompt: str, knowledge: str, history: list[str], frame_bgr: np.ndarray) -> dict:
        """给定任务、知识卡、最近步骤历史和当前帧，返回下一个动作 JSON。"""
        lines = [f"# 当前任务\n{task_prompt}"]
        if knowledge:
            lines.append(f"# 任务知识卡（历史探索结论，优先参考）\n{knowledge}")
        if history:
            lines.append("# 最近步骤（已从上下文裁剪，仅保留近几步）\n" + "\n".join(history))
        lines.append("# 当前画面\n请给出下一步操作的 JSON。")
        content = [{"type": "text", "text": "\n\n".join(lines)}, self._image_part(frame_bgr)]
        try:
            return self._parse_json(self._chat(content))
        except BrainError:
            # 一次纠正重试：要求严格只输出 JSON
            retry = content + [{
                "type": "text",
                "text": "你上一次的输出无法解析为 JSON。请严格只输出一个 JSON 对象"
                        "（action 为 click/wait/wait_stable/report 之一），不要任何其他文字。",
            }]
            return self._parse_json(self._chat(retry))

    def verify(self, task_prompt: str, exit_condition: str, frame_bgr: np.ndarray) -> tuple[bool, str]:
        """完成验证：用一张新鲜截图独立判断是否满足退出条件（与决策请求分离）。"""
        prompt = (
            f"# 任务\n{task_prompt}\n\n# 完成判据\n{exit_condition}\n\n"
            "# 请仅根据当前画面判断任务是否已完成，只输出 JSON：\n"
            '{"verified": true或false, "reason": "依据画面可见证据的简短说明"}'
        )
        text = self._chat(
            [{"type": "text", "text": prompt}, self._image_part(frame_bgr)],
            system="你是游戏画面验收员，只依据画面可见证据判断，不做任何操作。",
        )
        data = self._parse_json(text)
        return bool(data.get("verified")), str(data.get("reason", ""))

    def summarize_knowledge(self, task_name: str, old_knowledge: str, history: list[str]) -> str:
        """任务完成后把本次探索过程压缩成知识卡要点（用户的"打标成本转嫁给模型"）。"""
        prompt = (
            f"# 任务：{task_name}\n\n# 旧知识卡\n{old_knowledge or '（无）'}\n\n"
            f"# 本次执行步骤\n" + "\n".join(history) + "\n\n"
            "# 请输出更新后的知识卡：保留旧卡中仍有效的要点，补充本次新发现的路径要点/坑位"
            "（按钮位置描述、日文词、需要等待的动画等）。用简短条目列表，不超过 12 条，只输出列表本身。"
        )
        return self._chat([{"type": "text", "text": prompt}]).strip()

    def read_json_from_image(self, frame_bgr: np.ndarray, instruction: str) -> dict:
        """对（裁剪过的）局部截图做定向识读，返回模型给出的 JSON。

        用于确定性前置检查（如读挂机倒计时），prompt 必须要求只输出 JSON。
        """
        text = self._chat(
            [{"type": "text", "text": instruction}, self._image_part(frame_bgr)],
            system="你是画面识读助手，只输出一个 JSON 对象，禁止输出其他文字。",
        )
        return self._parse_json(text)

    def select_flow_steps(self, record_lines: list[str]) -> dict:
        """从探索执行记录中挑选『最短正确路径』的关键点击步骤，并判断探索是否退化。

        record_lines 形如 "step3: click(823,648) eff=0.31｜点击冒险按钮"。
        退化 = 这次探索没有发生任务的核心动作（领取/战斗/提交等），
        只是"进入→发现空/不可用→退出"的空走——此时生成的剧本是无效的。
        模型只做语义挑选与命名；坐标、锚点裁剪由框架完成。
        """
        prompt = (
            "以下是一次游戏任务自动探索的完整执行记录。eff 是该次点击后画面的变化率"
            "（<0.02 说明点击没生效，多半是试错或误点）。\n"
            "请完成两件事：\n"
            "1) 判断这次探索是否『退化』：即没有执行任务的核心动作（领取奖励、打完战斗、"
            "提交物品等），只是进入界面发现为空/不可用后退出。若是，degenerate=true。\n"
            "2) 若未退化，选出构成『从起点到任务完成的最短正确路径』的关键点击步骤：\n"
            "   - 跳过无效点击（eff 很低）、重复尝试中失败的那些、纯等待；\n"
            "   - 同一目的的多次点击只保留成功生效的那次；按执行顺序输出。\n"
            '只输出 JSON：{"degenerate": bool, "reason": "一句话说明",\n'
            '  "steps": [{"ref_step": 步骤号(int), "name": "简短中文名(不超过14字)"}]}\n\n'
            + "\n".join(record_lines)
        )
        data = self._parse_json(
            self._chat(
                [{"type": "text", "text": prompt}],
                system="你是自动化流程分析师，只输出一个 JSON 对象，禁止输出其他文字。",
            )
        )
        return {
            "degenerate": bool(data.get("degenerate", False)),
            "reason": str(data.get("reason", "")),
            "steps": list(data.get("steps", [])),
        }
