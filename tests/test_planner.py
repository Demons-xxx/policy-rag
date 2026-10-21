"""查询拆解与子问题配额的单测（纯规则，不加载模型）。"""
from app.retrieval import planner
from app.retrieval.ranking import enforce_quotas

TITLES = ['XX大学科研经费报销实施细则', 'XX大学实验室安全管理办法（2025年修订）',
          'XX大学教职工请假与考勤管理规定']


def test_single_topic_question_keeps_original_text():
    plan = planner.plan_query('住宿费标准是多少？', titles=TITLES)
    assert plan.is_multi is False
    assert plan.sub_queries == ('住宿费标准是多少?',) or plan.sub_queries == ('住宿费标准是多少？',)


def test_split_on_fenbie_yields_aspects():
    plan = planner.plan_query('用科研经费购买实验室危险化学品，采购和报销分别要遵守什么规定？', titles=TITLES)
    assert plan.is_multi
    assert any('报销' in item for item in plan.sub_queries)
    assert any('采购' in item for item in plan.sub_queries)


def test_split_on_tongshi_keeps_both_actions():
    plan = planner.plan_query('新入职教师进实验室前要培训多久，同时要请假，需要几天？', titles=TITLES)
    assert plan.is_multi
    assert any('培训' in item for item in plan.sub_queries)
    assert any('请假' in item for item in plan.sub_queries)


def test_aspect_variant_focuses_short_item():
    plan = planner.plan_query('用科研经费购买实验室危险化学品，采购和报销分别要遵守什么规定？', titles=TITLES)
    assert any(item.startswith('危险化学品 采购') for item in plan.sub_queries)


def test_stop_parts_are_dropped():
    plan = planner.plan_query('实验室安全事故和教职工请假扣考勤分别由哪个部门归口管理？', titles=TITLES)
    assert all(item not in planner.STOP_PARTS for item in plan.sub_queries)


def test_strip_unknown_doc_names_only_removes_fabricated_ones():
    text = '出差报销单提交时限 公务出差费用报销办法 科研经费报销实施细则'
    result = planner.strip_unknown_docs(text, TITLES)
    assert '公务出差费用报销办法' not in result
    assert '科研经费报销实施细则' in result


def test_strip_unknown_docs_keeps_question_phrases():
    text = '采购和报销分别要遵守什么规定'
    assert planner.strip_unknown_docs(text, TITLES) == text


def test_enforce_quotas_inserts_each_group_best():
    ordered = ['u1', 'u2', 'u3', 'u4', 'u5']
    selection = ['u1', 'u2', 'u3', 'u4', 'u5']
    quotas = [['x1', 'x2'], ['u2', 'x3']]
    result = enforce_quotas(selection, ordered, quotas, top_k=5)
    assert 'x1' in result            # 第一组的最佳被换入
    assert 'u2' in result            # 第二组已有代表，不需要换
    assert len(result) == 5
    assert 'u5' not in result        # 换掉的是全局最后一名


def test_enforce_quotas_keeps_order_of_ordered():
    ordered = ['u1', 'u2', 'u3', 'u4', 'x1']
    selection = ['u1', 'u2', 'u3', 'u4', 'x9']
    result = enforce_quotas(selection, ordered, [['x1']], top_k=5)
    assert result == ['u1', 'u2', 'u3', 'u4', 'x1']
