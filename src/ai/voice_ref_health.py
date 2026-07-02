"""参考音「体检」——把一段克隆参考录音量化成「能不能克隆得像」的红黄绿灯 + 可执行建议。

为什么需要：克隆真实感**七成靠参考音质量**。用户常踩的坑是机械、可量化、可一句话纠正的：
录得太短（音色采样不足）、削波破音（过载失真烙进音色）、大段静音/留白（有效人声太少）。
本模块只对这些**零误报、可执行**的维度打分；噪声/混响这类需谱分析、易误判的留作后续。

设计原则（与 voice_emotion / voice_clone_client 的 build_* 同风格）：
  - **纯函数、无 IO/网络**：入参是已解码的单声道浮点样本 + 采样率 → 出一个 dict，可单测。
  - **防御式**：任何脏输入（空/NaN/怪类型）→ 安全降级（``unknown`` 或 ``red`` + 提示），绝不抛异常。
  - **零误报优先**：宁可漏报噪声，不可把一段干净录音误判成坏（误报会让用户不信任体检）。

输出 schema（全部字段恒在）::

    {
      "grade": "green|yellow|red|unknown",  # 总评（取各维度最差）
      "score": 0..100,                       # 便于排序/展示
      "summary": "质量良好，适合克隆",         # 一句话人话
      "duration_sec": 9.3,
      "clip_ratio": 0.001,                   # 满幅样本占比（削波）
      "silence_ratio": 0.18,                 # 近静音帧占比（相对峰值）
      "peak_dbfs": -1.2,
      "noise_floor_dbfs": -52.0,             # 最安静 10% 帧均值（信息项，不参与评级）
      "issues": ["录音过短"],                  # 命中的问题短标签
      "hints": ["太短了，建议 6–15 秒连续清晰人声"],  # 对应可执行建议
    }
"""
from __future__ import annotations

import math
from typing import Any, Dict

# 建议时长窗口（秒）：< MIN 太短、(MIN,LOW) 略短、(HIGH,MAX) 偏长、> MAX 仅取前 MAX。
_DUR_MIN = 3.0
_DUR_LOW = 5.0
_DUR_HIGH = 18.0
_DUR_MAX = 20.0

_GRADE_RANK = {"green": 0, "yellow": 1, "red": 2}
_SUMMARY = {
    "green": "质量良好，适合克隆",
    "yellow": "可用，按提示优化会更像",
    "red": "质量不佳，建议按提示重录",
    "unknown": "无法评估",
}
# green 态也给一条「锦上添花」的环境提示（噪声/混响不易量化，用教育代替误报）。
_GREEN_TIP = "已不错～环境越安静、单人清晰朗读，克隆越像"


def _dbfs(x: float) -> float:
    """线性幅度 → dBFS；0/负 → 很小值，避免 -inf。"""
    try:
        return round(20.0 * math.log10(max(float(x), 1e-9)), 1)
    except Exception:
        return -120.0


def _blank(grade: str = "unknown") -> Dict[str, Any]:
    return {
        "grade": grade, "score": 0 if grade != "green" else 100,
        "summary": _SUMMARY.get(grade, ""),
        "duration_sec": 0.0, "clip_ratio": 0.0, "silence_ratio": 0.0,
        "peak_dbfs": -120.0, "noise_floor_dbfs": -120.0,
        "issues": [], "hints": [],
    }


def analyze_reference_audio(samples: Any, sr: int, *, max_len_sec: float = _DUR_MAX) -> Dict[str, Any]:
    """对单声道浮点样本（[-1,1]）做参考音体检。任何异常 → ``unknown``（绝不抛）。

    评级维度（零误报）：时长 / 削波 / 静音占比。噪声仅作信息项（``noise_floor_dbfs``）。
    削波须在**未归一**的原始音上测才准（归一后峰值被压到 0.97，满幅信息丢失）。
    """
    out = _blank("unknown")
    try:
        import numpy as np
    except Exception:
        return out
    try:
        arr = np.asarray(samples, dtype="float64").reshape(-1)
        sr = int(sr) if sr else 0
        n = int(arr.size)
        if n == 0 or sr <= 0:
            o = _blank("red"); o["issues"] = ["空音频"]
            o["hints"] = ["没读到有效音频，换个文件再试"]; o["summary"] = "无法读取音频"
            return o
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(arr, -1.0, 1.0, out=arr)
        dur = n / float(sr)
        absx = np.abs(arr)
        peak = float(absx.max())
        out["duration_sec"] = round(dur, 1)
        out["peak_dbfs"] = _dbfs(peak)
        if peak < 1e-3:                       # 整体近乎无声
            o = _blank("red"); o["duration_sec"] = round(dur, 1)
            o["silence_ratio"] = 1.0; o["peak_dbfs"] = out["peak_dbfs"]
            o["issues"] = ["几乎无声"]; o["hints"] = ["这段几乎没有声音，确认录到人声再上传"]
            o["summary"] = _SUMMARY["red"]; o["score"] = 0
            return o

        clip_ratio = float(np.mean(absx >= 0.99))
        out["clip_ratio"] = round(clip_ratio, 4)

        # 20ms 帧 RMS → 静音占比（相对峰值，尺度无关，归一前后一致）+ 噪声地板（信息项）
        fl = max(1, int(sr * 0.02))
        nf = n // fl
        if nf >= 1:
            frames = arr[: nf * fl].reshape(nf, fl)
            frms = np.sqrt(np.mean(frames * frames, axis=1))
        else:
            frms = np.array([float(np.sqrt(np.mean(arr * arr)))])
        sil_thresh = peak * 0.02              # 低于峰值 2%(-34dB) 视作近静音
        silence_ratio = float(np.mean(frms < sil_thresh))
        out["silence_ratio"] = round(silence_ratio, 3)
        quiet_n = max(1, int(len(frms) * 0.1))
        noise_floor = float(np.mean(np.sort(frms)[:quiet_n]))
        out["noise_floor_dbfs"] = _dbfs(noise_floor)

        # ── 评级（每维度 → (grade, issue, hint)）──────────────────────────
        checks = []
        if dur < _DUR_MIN:
            checks.append(("red", "录音过短", "太短了，建议 6–15 秒连续清晰人声，克隆才稳"))
        elif dur < _DUR_LOW:
            checks.append(("yellow", "录音略短", "建议 8–15 秒，采样更足、音色更稳"))
        elif dur > _DUR_HIGH:
            checks.append(("yellow", "录音偏长", "只会用前 %d 秒，8–15 秒即可" % int(max_len_sec)))

        if clip_ratio > 0.02:
            checks.append(("red", "削波破音", "录音过载有破音，调低录音电平/离麦远点重录"))
        elif clip_ratio > 0.005:
            checks.append(("yellow", "轻微削波", "音量偏大略有破音，下次小声一点"))

        if silence_ratio > 0.7:
            checks.append(("red", "有效人声过少", "大段静音/留白，剪掉空白、多保留说话"))
        elif silence_ratio > 0.5:
            checks.append(("yellow", "静音偏多", "句间留白有点多，剪短停顿会更好"))

        grade = "green"
        issues, hints = [], []
        for g, iss, h in checks:
            issues.append(iss); hints.append(h)
            if _GRADE_RANK[g] > _GRADE_RANK[grade]:
                grade = g
        if grade == "green" and not hints:
            hints = [_GREEN_TIP]

        nred = sum(1 for g, _, _ in checks if g == "red")
        nyellow = sum(1 for g, _, _ in checks if g == "yellow")
        out["grade"] = grade
        out["issues"] = issues
        out["hints"] = hints
        out["score"] = max(0, min(100, 100 - 45 * nred - 17 * nyellow))
        out["summary"] = _SUMMARY[grade]
        return out
    except Exception:
        return out


__all__ = ["analyze_reference_audio"]
