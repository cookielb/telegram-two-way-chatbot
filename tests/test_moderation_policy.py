from types import SimpleNamespace
import pytest
from services.moderation_rules import validate_result, message_text, quick_advertising_check


def verdict(score, spam=False, category='safe'):
    return dict(is_spam=spam, risk_score=score, category=category, reason='依据')


def test_clear_advertising_is_blocked():
    assert validate_result(verdict(90, True, 'advertising'), 70, 40)['status'] == 'blocked'


def test_uncertain_advertising_goes_to_review():
    assert validate_result(verdict(45, True, 'advertising'), 70, 40)['status'] == 'review'


def test_normal_question_passes():
    assert validate_result(verdict(10), 70, 40)['status'] == 'passed'


def test_new_user_threshold_is_stricter():
    assert validate_result(verdict(30), 70, 40)['status'] == 'passed'
    assert validate_result(verdict(30), 70, 25)['status'] == 'review'


@pytest.mark.parametrize('bad', [None, {}, verdict(True), verdict(101),
    dict(is_spam='false', risk_score=0, category='safe', reason='依据')])
def test_malformed_verdict_cannot_pass(bad):
    with pytest.raises(ValueError):
        validate_result(bad, 70, 40)


def test_hidden_links_are_included():
    message = SimpleNamespace(text='点这里', caption=None,
        entities=[SimpleNamespace(url='https://example.com/promo')], caption_entities=[])
    assert 'https://example.com/promo' in message_text(message)


def test_link_alone_does_not_trigger_local_rule():
    assert quick_advertising_check('请问 https://example.com 是否能访问？') is None


def test_marketing_and_contact_are_quarantined():
    result = quick_advertising_check('稳赚不赔，联系 @someone123')
    assert result['status'] == 'review'


def test_business_advertising_verdict_requires_review():
    from services.moderation_rules import protect_business_inquiry
    result = validate_result(verdict(90, True, "advertising"), 70, 40)
    assert protect_business_inquiry(result, "想上车合租 VPS，多少钱？")["status"] == "review"


def test_business_words_do_not_whitelist_scam():
    from services.moderation_rules import protect_business_inquiry
    result = validate_result(verdict(95, True, "scam"), 70, 40)
    assert protect_business_inquiry(result, "VPS 合租，保证投资收益")["status"] == "blocked"


def test_placeholder_response_is_rejected():
    from services.moderation_rules import validate_result
    with pytest.raises(ValueError):
        validate_result({"is_spam": False, "risk_score": 0, "category": "safe", "reason": "等待待审文本输入。"}, 70, 40)
