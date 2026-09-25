"""Silero VAD 封裝：純 onnxruntime + numpy，不依賴 torch。

見 ARCHITECTURE.md §7：VAD 存在的目的不是省算力，是抑制 Whisper 幻覺
（對靜音段做 ASR 會產生憑空文字）。audio-service 是純 CPU、高優先權、
永不阻塞的進程，特意不 import torch——torch 的 import 時間與記憶體開銷
跟這支進程的角色不搭（見 requirements.txt 的說明）。

模型 I/O 契約（從 silero-vad 官方 wheel 的 `OnnxWrapper` 逆向出來，
見 scripts/setup_models.py 的下載腳本）：

    輸入
        input: float32[1, 576]  -- 64 樣本前情脈絡 + 512 樣本新音框
        state: float32[2, 1, 128]  -- 遞迴狀態，逐框傳遞
        sr:    int64 純量，這裡固定 16000
    輸出
        out:   float32[1, 1]  -- 這一框是語音的機率 (0~1)
        state: float32[2, 1, 128]  -- 更新後的遞迴狀態，餵給下一框

模型只接受剛好 512 個樣本一框（16kHz 下 32ms）。呼叫端要自己把音訊切成
512 樣本的框——這正是 `VadStream.process()` 的責任。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort

FRAME_SAMPLES = 512  # 16kHz 下固定 32ms，模型硬性要求，不可變
CONTEXT_SAMPLES = 64
SAMPLE_RATE = 16000

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "silero_vad.onnx"


class SileroVAD:
    """逐框（512 樣本）餵音訊，回傳語音機率。狀態在物件內部遞迴傳遞。"""

    def __init__(self, model_path: Path | str = DEFAULT_MODEL_PATH) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise FileNotFoundError(
                f"找不到 VAD 模型: {model_path}\n"
                f"先跑 `.venv/Scripts/python.exe scripts/setup_models.py` 下載"
            )
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        providers = (
            ["CPUExecutionProvider"]
            if "CPUExecutionProvider" in ort.get_available_providers()
            else None
        )
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=providers
        )
        self.reset()

    def reset(self) -> None:
        """新的一段音訊開始前呼叫（例如換了擷取來源），清掉遞迴狀態。"""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def process_frame(self, frame: np.ndarray) -> float:
        """餵剛好 `FRAME_SAMPLES` 個 float32 樣本，回傳這一框是語音的機率。"""
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(
                f"SileroVAD 只接受剛好 {FRAME_SAMPLES} 個樣本一框，收到 shape={frame.shape}"
            )

        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        ort_inputs = {
            "input": x,
            "state": self._state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        }
        prob, new_state = self._session.run(None, ort_inputs)

        self._state = new_state
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(prob.reshape(-1)[0])


class VadStream:
    """把任意長度的音訊流餵進來，內部自己切成 512 樣本一框，逐框回傳機率。

    呼叫端（segmenter.py）不需要知道 512 這個數字——`push()` 可以餵任意
    長度，剩餘不足一框的樣本會留到下次呼叫再湊。
    """

    def __init__(self, model_path: Path | str = DEFAULT_MODEL_PATH) -> None:
        self._vad = SileroVAD(model_path)
        self._pending = np.empty(0, dtype=np.float32)

    def reset(self) -> None:
        self._vad.reset()
        self._pending = np.empty(0, dtype=np.float32)

    @property
    def pending_samples(self) -> int:
        """已收進來、但還湊不滿一框所以尚未算過的樣本數（0..FRAME_SAMPLES-1）。

        換音源時，呼叫端要補零把這段湊滿一框再 reset，讓「VAD/segmenter 處理過
        的位置」與 ring buffer 的寫入位置重新對齊。
        """
        return len(self._pending)

    def push(self, samples: np.ndarray) -> list[float]:
        """回傳這次呼叫湊滿的每一框機率（可能是空 list，也可能不只一個）。"""
        buf = np.concatenate([self._pending, samples.astype(np.float32)])
        n_frames = len(buf) // FRAME_SAMPLES
        probs = [
            self._vad.process_frame(buf[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES])
            for i in range(n_frames)
        ]
        self._pending = buf[n_frames * FRAME_SAMPLES :]
        return probs
