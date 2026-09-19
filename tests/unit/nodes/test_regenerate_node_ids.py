from dynamiq.flows import Flow
from dynamiq.nodes import InputTransformer
from dynamiq.nodes.cloning import regenerate_node_ids
from dynamiq.nodes.node import NodeDependency, NodeOutputReference
from dynamiq.nodes.operators import Choice, ChoiceOption, DecisionTable, Pass, Rules, SubWorkflow
from dynamiq.nodes.types import ChoiceCondition, ConditionOperator, DecisionRule, DerivedValue, NamedField, Rule
from dynamiq.nodes.utils import Input


def test_a_node_reachable_twice_gets_one_new_id_and_id_paths_follow_it():
    first = Pass(id="first", name="first")
    second = Pass(
        id="second",
        name="second",
        depends=[NodeDependency(node=first)],
        input_transformer=InputTransformer(
            path="$.first.output", selector={"x": "$.first.output.x || $.first.output.y"}
        ),
    )
    node = SubWorkflow(id="sub", name="sub", flow=Flow(id="flow", nodes=[first, second]))

    id_map: dict[str, set[str]] = {}
    clone = regenerate_node_ids(node.clone(), id_map)

    cloned_first, cloned_second = clone.flow.nodes
    assert cloned_second.depends[0].node is cloned_first
    assert id_map["first"] == {cloned_first.id}
    assert cloned_second.input_transformer.path == f'$."{cloned_first.id}".output'
    assert cloned_second.input_transformer.selector == {
        "x": f'$."{cloned_first.id}".output.x || $."{cloned_first.id}".output.y'
    }
    # The original is untouched.
    assert first.id == "first" and second.input_transformer.path == "$.first.output"

    # A second pass, as under a Map inside a Map, moves the paths on again.
    again = regenerate_node_ids(clone.clone(), {})
    again_first, again_second = again.flow.nodes
    assert again_first.id != cloned_first.id
    assert again_second.input_transformer.path == f'$."{again_first.id}".output'
    assert (
        again_second.input_transformer.selector["x"]
        == f'$."{again_first.id}".output.x || $."{again_first.id}".output.y'
    )


def test_a_dependency_gated_on_a_choice_option_follows_the_option_id():
    route = Choice(
        id="route",
        name="route",
        options=[
            ChoiceOption(
                id="opt-hi",
                condition=ChoiceCondition(
                    operator=ConditionOperator.NUMERIC_GREATER_THAN, variable="$.score", value=50
                ),
            ),
            ChoiceOption(id="opt-lo"),
        ],
    )
    hi = Pass(id="hi", name="hi", depends=[NodeDependency(node=route, option="opt-hi")])
    node = SubWorkflow(id="sub", name="sub", flow=Flow(id="flow", nodes=[route, hi]))

    clone = regenerate_node_ids(node.clone(), {})

    cloned_route, cloned_hi = clone.flow.nodes
    assert cloned_route.options[0].id != "opt-hi"
    assert cloned_hi.depends[0].option == cloned_route.options[0].id
    assert hi.depends[0].option == "opt-hi"


def test_a_choice_condition_naming_a_node_by_id_follows_the_new_id():
    start = Input(id="start", name="start")
    route = Choice(
        id="route",
        name="route",
        options=[
            ChoiceOption(
                id="opt-hi",
                condition=ChoiceCondition(
                    operands=[
                        ChoiceCondition(
                            operator=ConditionOperator.NUMERIC_GREATER_THAN, variable="$.start.output.score", value=50
                        )
                    ],
                    operator=ConditionOperator.AND,
                ),
            ),
            ChoiceOption(id="opt-lo"),
        ],
        depends=[NodeDependency(node=start)],
    )
    node = SubWorkflow(id="sub", name="sub", flow=Flow(id="flow", nodes=[start, route]))

    clone = regenerate_node_ids(node.clone(), {})

    cloned_start, cloned_route = clone.flow.nodes
    assert cloned_start.id != "start"
    assert cloned_route.options[0].condition.operands[0].variable == f'$."{cloned_start.id}".output.score'
    assert route.options[0].condition.operands[0].variable == "$.start.output.score"


def test_a_flow_copy_relinks_output_references_to_the_copied_nodes():
    start = Pass(id="start", name="start")
    calc = Pass(
        id="calc",
        name="calc",
        depends=[NodeDependency(node=start)],
        input_mapping={"score": NodeOutputReference(node=start, output_key="score"), "scale": 2},
    )
    flow = Flow(id="flow", nodes=[start, calc])

    copied = flow.clone()

    copied_start, copied_calc = copied.nodes
    assert copied_calc.input_mapping["score"].node is copied_start
    assert copied_calc.input_mapping["scale"] == 2
    assert calc.input_mapping["score"].node is start


def test_paths_naming_a_column_or_an_option_rather_than_a_node_are_left_alone():
    table = DecisionTable(
        id="table",
        name="table",
        input_columns=[NamedField(id="fico", name="fico", type="int")],
        output_columns=[NamedField(id="decision", name="decision", type="string")],
        rules=[DecisionRule(id="r1", when=[">= 700"], then=["approve"])],
        input_transformer=InputTransformer(selector={"fico": "$.fico"}),
    )
    route = Choice(
        id="route",
        name="route",
        options=[ChoiceOption(id="query")],
        input_transformer=InputTransformer(selector={"q": "$.query"}),
    )
    node = SubWorkflow(id="sub", name="sub", flow=Flow(id="flow", nodes=[table, route]))

    clone = regenerate_node_ids(node.clone(), {})

    # The column keeps its id and the option carries a new one, but `$.fico` and `$.query` name input
    # keys, not nodes.
    cloned_table, cloned_route = clone.flow.nodes
    assert cloned_table.input_columns[0].id == "fico"
    assert cloned_route.options[0].id != "query"
    assert cloned_table.input_transformer.selector == {"fico": "$.fico"}
    assert cloned_route.input_transformer.selector == {"q": "$.query"}


def test_a_rule_a_row_and_a_field_keep_the_ids_the_user_wrote_where_the_node_gets_a_new_one():
    table = DecisionTable(
        id="pricing",
        input_columns=[NamedField(id="col-1", name="tier")],
        output_columns=[NamedField(id="col-2", name="rate")],
        rules=[DecisionRule(id="r-1", when=["gold"], then=[0.1])],
    )
    checks = Rules(
        id="review",
        input_fields=[NamedField(id="f-1", name="claim")],
        derived_values=[DerivedValue(id="d-1", name="total", expression="claim.amount")],
        rules=[Rule(id="POL-01", check="claim.amount < 1000")],
    )

    id_map: dict[str, set[str]] = {}
    table_copy = regenerate_node_ids(table.clone(), id_map)
    checks_copy = regenerate_node_ids(checks.clone(), id_map)

    assert table_copy.id != "pricing" and checks_copy.id != "review"
    assert set(id_map) == {"pricing", "review"}
    assert [rule.id for rule in table_copy.rules] == ["r-1"]
    assert [column.id for column in table_copy.input_columns + table_copy.output_columns] == ["col-1", "col-2"]
    assert [rule.id for rule in checks_copy.rules] == ["POL-01"]
    assert [field.id for field in checks_copy.input_fields] == ["f-1"]
    assert [value.id for value in checks_copy.derived_values] == ["d-1"]
    assert table_copy.to_dict(for_tracing=True)["rules"][0]["id"] == "r-1"
