"""出站情感声「回应听到的语气」门禁：derive_emotion 的 peer_audio_emotion 信号。

对方声学情绪 → 我方**回应式**情绪（对方难过→温柔共情、生气→歉意安抚、恐惧→沉稳），
且不镜像（我们不跟着难过）；低置信/中性不干预，行为回退既有逻辑。
"""
from src.ai.speech_emotion import map_audio_emotion
from src.ai.voice_emotion import EmotionSpec, derive_emotion


def test_peer_sad_drives_empathetic():
    pae = map_audio_emotion("sad", 0.85)
    spec = derive_emotion(text="随便啦", peer_audio_emotion=pae)
    assert isinstance(spec, EmotionSpec)
    assert spec.emotion == "empathetic"
    assert spec.pace == "slow"


def test_peer_angry_drives_apologetic():
    pae = map_audio_emotion("angry", 0.8)
    spec = derive_emotion(text="嗯", peer_audio_emotion=pae)
    assert spec.emotion == "apologetic"


def test_peer_fearful_drives_calm():
    pae = map_audio_emotion("fearful", 0.8)
    spec = derive_emotion(text="嗯", peer_audio_emotion=pae)
    assert spec.emotion == "calm"


def test_low_confidence_peer_does_not_override():
    pae = map_audio_emotion("sad", 0.3)  # confident False
    # 无其它线索 → 落到 default warm（未被声学干预）
    spec = derive_emotion(text="嗯", peer_audio_emotion=pae, default="warm")
    assert spec.emotion == "warm"


def test_neutral_peer_does_not_override_text_cue():
    pae = map_audio_emotion("neutral", 0.95)
    # 文本线索「哈哈」→ playful，声学中性不应干预
    spec = derive_emotion(text="哈哈哈笑死", peer_audio_emotion=pae)
    assert spec.emotion == "playful"


def test_no_peer_audio_behaves_as_before():
    spec = derive_emotion(text="谢谢你", peer_audio_emotion=None)
    assert spec.emotion == "warm"  # 文本线索「谢谢」→ warm（既有行为）
