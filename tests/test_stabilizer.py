"""inference/stabilizer.py 的測試，對應 ARCHITECTURE.md §8 的例子。"""

from __future__ import annotations

from inference.stabilizer import Stabilizer


def test_first_update_always_returns_zero() -> None:
    """第一次解碼沒有東西可以比對，不該有任何穩定前綴。"""
    s = Stabilizer(granularity="word")
    assert s.update("I think we should") == 0


def test_matches_architecture_example() -> None:
    """直接照 ARCHITECTURE.md §8 的例子驗證。"""
    s = Stabilizer(granularity="word")

    s.update("I think we should")
    stable_len = s.update("I think we shall consider")
    # 共同前綴是 "I think we"（3 個詞），join 後長度 = len("I think we")
    assert stable_len == len("I think we")

    stable_len = s.update("I think we shall consider the")
    # 前綴延伸到 "I think we shall consider"
    assert stable_len == len("I think we shall consider")


def test_fully_identical_consecutive_decodes_are_fully_stable() -> None:
    s = Stabilizer(granularity="word")
    s.update("hello world")
    stable_len = s.update("hello world")
    assert stable_len == len("hello world")


def test_completely_different_decodes_have_zero_stable_prefix() -> None:
    s = Stabilizer(granularity="word")
    s.update("hello world")
    stable_len = s.update("goodbye moon")
    assert stable_len == 0


def test_char_granularity_for_languages_without_spaces() -> None:
    """日文/泰文特例：比對粒度要用字元，見 §8「泰文特例」。"""
    s = Stabilizer(granularity="char")
    s.update("私は学校に")
    stable_len = s.update("私は学校に行きます")
    assert stable_len == len("私は学校に")


def test_char_granularity_does_not_break_on_partial_word_match() -> None:
    """字元層級比對時，"わ" 這種只多打一半的詞也能算進穩定前綴——
    這正是不能用空格切詞的語言需要字元粒度的原因。"""
    s = Stabilizer(granularity="char")
    s.update("こんにち")
    stable_len = s.update("こんにちは")
    assert stable_len == len("こんにち")


def test_shrinking_hypothesis_only_common_prefix_counts() -> None:
    """後面那次解碼比前一次短：穩定前綴只能到較短的那個為止。"""
    s = Stabilizer(granularity="word")
    s.update("this is a long sentence")
    stable_len = s.update("this is a")
    assert stable_len == len("this is a")


def test_reset_clears_state_for_new_utterance() -> None:
    s = Stabilizer(granularity="word")
    s.update("first utterance text")
    s.reset()

    # reset 後應該表現得像全新物件：第一次呼叫回傳 0
    assert s.update("second utterance") == 0


def test_word_granularity_empty_string() -> None:
    s = Stabilizer(granularity="word")
    assert s.update("") == 0
    assert s.update("") == 0  # 兩次都空字串，"共同前綴" 也是空
