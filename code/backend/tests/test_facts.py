from app.agents.facts import FactTable, check_numbers


def _facts() -> FactTable:
    f = FactTable()
    f.add("window.days", 7)
    f.add("g_haiyan.visitors_cur", 2491)
    f.add("g_haiyan.cvr_cur_pct", 2.2)
    f.add("g_haiyan.cvr_prev_pct", 4.1)
    f.add("g_citrus.cvr_cur_pct", 5.2)
    f.ignore_strings.add("海盐香薰蜡烛 200g")
    return f


def test_numbers_from_facts_pass():
    text = "因为2026-10-17主图换成白底图后，近7天转化率从4.1%降到2.2%，访客2491"
    assert check_numbers(text, _facts()) == []


def test_fabricated_number_fails():
    assert check_numbers("转化率下降到 1.3%", _facts()) == [1.3]


def test_derived_values_within_group_pass():
    # 4.1 − 2.2 = 1.9 个百分点；(4.1 − 2.2) / 4.1 ≈ 46.3%
    assert check_numbers("下降 1.9 个百分点，降幅 46.3%", _facts()) == []


def test_derived_values_across_unrelated_groups_fail():
    # 2491 − 5.2 不属于同一对象也不是同一指标，不允许
    assert check_numbers("差值 2485.8", _facts()) == [2485.8]


def test_ignore_strings_and_dates():
    assert check_numbers("海盐香薰蜡烛 200g 在10月17日换图", _facts()) == []
