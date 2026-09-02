"""任务前置条件检查：用确定性判据决定任务是否可以跳过。

当前支持 idle_timer：读主页面右下角挂机宝箱的"MAXまで"倒计时（16 小时满），
剩余时间大于阈值说明刚领过，跳过领取任务。
"""
from .config import WINDOW_SIZE
from .device import GameDevice
from .brain import Brain

IDLE_TIMER_PROMPT = (
    "这是游戏画面右下角的倒计时文字区域，格式类似『MAXまで 1:47』（H:MM 或 MM:SS，"
    "表示距离挂机奖励攒满剩余的时间，满值 16 小时）。"
    "请识读并只输出 JSON："
    '{"raw": "看到的原文", "hours_remaining": 数字（换算成小时，如 1.78；'
    "若没有看到任何倒计时文字则为 null）}"
)


def check_precondition(task: dict, device: GameDevice, brain: Brain) -> tuple[bool, str]:
    """返回 (是否跳过, 原因)。"""
    pre = task.get("precondition")
    if not pre:
        return False, ""

    ptype = pre.get("type")
    if ptype == "idle_timer":
        frame = device.screenshot()
        x0, y0, x1, y1 = pre["region"]
        h_img, w_img = frame.shape[:2]
        x1 = min(x1, w_img)
        y1 = min(y1, h_img)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return False, "裁剪区域无效，保守执行"
        data = brain.read_json_from_image(crop, IDLE_TIMER_PROMPT, scene="precondition")
        hours = data.get("hours_remaining")
        if hours is None:
            return False, f"未读到倒计时（raw={data.get('raw')!r}），保守执行"
        hours = float(hours)
        threshold = float(pre.get("skip_if_remaining_above_hours", 14.0))
        if hours > threshold:
            return True, f"挂机剩余 {hours:.2f}h > 阈值 {threshold}h，刚领过，跳过"
        return False, f"挂机剩余 {hours:.2f}h ≤ 阈值 {threshold}h，需要领取"

    return False, f"未知前置条件类型 {ptype}，保守执行"
