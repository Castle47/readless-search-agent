import pytest

from research_agent.services.assertion_policy import ordinary_explanation


@pytest.mark.parametrize("text", [
    "原始 Transformer 采用位置编码。",
    "这款产品的官方接口保证返回 JSON。",
    "实验结果的准确率为 95%。",
    "The original architecture uses six layers.",
    "The measured throughput is 100 tokens per second.",
])
def test_core_claim_cannot_be_relabelled(text):
    assert not ordinary_explanation(text, "explanation")


def test_ordinary_comparison_is_not_original_authorship_or_design():
    assert ordinary_explanation(
        "现代语言模型通常不是“原始 Transformer 本身”，而是采用了 Transformer 的某种架构变体。",
        "explanation",
    )
    assert ordinary_explanation("编码器型模型侧重理解完整输入。", "explanation")
    assert not ordinary_explanation("编码器型模型侧重理解完整输入。", "core_fact")
