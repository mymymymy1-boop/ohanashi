# -*- coding: utf-8 -*-
"""
お話の記憶 PRO — コンテンツスキーマ (pydantic v2)

docs/handoff/schemas/content_schemas.json の story_skeleton / story_text / question を
Pythonモデルに落としたもの。生成パイプラインの各段でこのモデルによる検証を通す。

注意:
- 数値レンジ等の詳細ルール(文字数・設問数マトリクス等)はスキーマではなく
  pipeline/qc.py (qc_rules.md 準拠) が判定する。ここでは構造と型のみ厳密にする。
- extra="allow" にして未知フィールドは落とさない(モデル出力の付加情報を保存するため)。
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Group = Literal["A", "B", "C", "D", "E"]
Season = Literal["haru", "natsu", "aki", "fuyu"]
Weather = Literal["hare", "kumori", "ame", "yuki"]
Color = Literal["aka", "ao", "kiiro", "midori"]          # 持ち物・帽子などの属性色 (§2.1)
InstructionColor = Literal["aka", "ao", "midori", "kuro"]  # 指示色は黒あり・黄なし (§1.3/schema)
Mark = Literal["maru", "sankaku", "shikaku", "batsu"]
QType = Literal["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10"]
DummyKind = Literal[
    "strong", "category", "numeric", "attribute_swap", "scene_composite", "emotion_set"
]
Emotion = Literal["ureshii", "kanashii", "okotta", "bikkuri", "sabishii", "waku waku"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------- story_skeleton ----------------

class CharacterAttrs(_Base):
    hat_color: Optional[Color] = None
    item: Optional[str] = None
    item_color: Optional[Color] = None


class Character(_Base):
    id: str
    display: Optional[str] = Field(None, description="うさぎさん 等の呼称")
    attrs: Optional[CharacterAttrs] = None


class Scene(_Base):
    seq: int = Field(ge=1)
    chars: List[str]
    event: str
    order_key: Optional[str] = None
    numbers: Dict[str, int] = Field(default_factory=dict)
    season_cue: Optional[str] = None
    life_common_sense: Optional[str] = None


class Quote(_Base):
    speaker: str
    text_key: str


class StateChange(_Base):
    obj: str
    from_: str = Field(alias="from")
    to: str

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class EmotionEntry(_Base):
    char: str
    emotion: Emotion
    then: Optional[Emotion] = None
    cause: Optional[str] = None


class StorySkeleton(_Base):
    skeleton_id: str = Field(pattern=r"^sk_[0-9]{8}_[0-9]{4}$")
    group: Group
    max_level: int = Field(ge=1, le=5)
    theme: str
    season: Season
    weather: Optional[Weather] = None
    seed_note: Optional[str] = None
    characters: List[Character] = Field(min_length=2)
    mentioned_absent: List[str] = Field(default_factory=list)
    scenes: List[Scene] = Field(min_length=1, max_length=10)
    dual_orders: Dict[str, List[str]] = Field(default_factory=dict)
    quotes: List[Quote] = Field(default_factory=list)
    state_changes: List[StateChange] = Field(default_factory=list)
    emotions: List[EmotionEntry] = Field(default_factory=list)

    @field_validator("scenes")
    @classmethod
    def _seq_sequential(cls, v: List[Scene]) -> List[Scene]:
        seqs = [s.seq for s in v]
        if seqs != sorted(seqs):
            raise ValueError("scenes の seq が昇順ではありません")
        return v


# ---------------- story_text ----------------

class SceneText(_Base):
    seq: int
    text: str


class VocabEntry(_Base):
    word: str
    gloss: Optional[str] = None


class StoryText(_Base):
    skeleton_id: str
    level: int = Field(ge=1, le=5)
    group: Group
    scenes_text: List[SceneText] = Field(min_length=1)
    char_count: int
    expected_duration_sec: float
    scene_boundaries: List[int] = Field(default_factory=list)
    new_vocab: List[VocabEntry] = Field(default_factory=list)
    speech_rate_cpm: Optional[int] = None

    def full_text(self) -> str:
        """scene結合済みの読み上げ本文(スペース・改行除去はしない素の連結)。"""
        return "".join(s.text for s in self.scenes_text)


# ---------------- question ----------------

class Instruction(_Base):
    mark: Mark
    color: InstructionColor
    multi: bool


class Choice(_Base):
    id: str
    image_key: str
    dummy_kind: Optional[DummyKind] = None


class Question(_Base):
    q_id: str
    skeleton_id: str
    level: int = Field(ge=1, le=5)
    type: QType
    instruction: Instruction
    prompt_text: str = Field(description="ひらがな設問文(音声化対象)")
    correct: List[str] = Field(min_length=1)
    choices: List[Choice] = Field(min_length=4, max_length=4)  # 全Lv4択(2026-07-25統一)
    time_limit_sec: int
    evidence_scene_seq: List[int] = Field(default_factory=list)


class QuestionSet(_Base):
    """1つの (skeleton, level) に対する設問セット。"""
    questions: List[Question] = Field(min_length=1)
