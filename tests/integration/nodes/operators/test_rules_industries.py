"""Five reviews from five industries, far apart, and the properties they share.

Insurance adjudicates a claim, a telecom desk stops a fraudulent port-out, a pharmacy checks a medication
order, a housing agency screens an application, and a plant releases a production batch. None of them is a
loan. Between them they cover a lookup by a key the record supplies, list membership and filters over lists of
records, dates against a fixed point in time, derived values that hold a dict, rules for one payer or one
request type, a policy that starts on a date, the strict and the lenient missing-value policy, a Rules node
inside a Map with an Expression folding the batch, routing on the outcome, and the YAML the platform stores.
The last tests are about the node itself: a rule that reaches for Python internals, five hundred rules against
the clock, and a batch across workers.
"""

import time

import pytest

from dynamiq import Workflow
from dynamiq.flows import Flow
from dynamiq.nodes import InputTransformer
from dynamiq.nodes.node import NodeDependency
from dynamiq.nodes.operators import Expression, Map, Rules
from dynamiq.nodes.types import DerivedValue, ExpressionItem, NamedField, Rule
from dynamiq.nodes.utils import Input, Output
from dynamiq.runnables import RunnableConfig, RunnableStatus


def run(node: Rules, record: dict) -> dict:
    result = node.run(input_data=record, config=RunnableConfig(callbacks=[]))
    assert result.status == RunnableStatus.SUCCESS, result.error
    return result.output


def by_id(output: dict) -> dict[str, dict]:
    return {finding["rule_id"]: finding for finding in output["findings"]}


def statuses(output: dict) -> dict[str, str]:
    return {rule_id: finding["status"] for rule_id, finding in by_id(output).items()}


# --- Insurance: a motor claim adjudicated against the policy and the claimant's history --------------------


def claim_adjudication() -> Rules:
    return Rules(
        id="adjudication",
        name="adjudication",
        input_fields=[NamedField(name="claim"), NamedField(name="policy"), NamedField(name="history")],
        derived_values=[DerivedValue(name="payable", expression="max(claim.estimate - policy.deductible, 0)")],
        rules=[
            Rule(
                id="POL-01",
                name="Policy in force on the loss date",
                check=(
                    "date(policy.effective_from) <= date(claim.loss_date)"
                    " and date(claim.loss_date) <= date(policy.effective_until)"
                ),
                message=(
                    "Loss on {{ claim.loss_date }}, policy in force"
                    " {{ policy.effective_from }} to {{ policy.effective_until }}"
                ),
            ),
            Rule(id="COV-01", name="Loss type covered", check="claim.loss_type in policy.coverages"),
            Rule(
                id="COV-02",
                name="Estimate within the coverage limit",
                severity="warn",
                check="claim.estimate <= policy.limits[claim.loss_type]",
                message=(
                    "Estimate {{ claim.estimate }} is above the {{ claim.loss_type }} limit"
                    " of {{ policy.limits[claim.loss_type] }}"
                ),
            ),
            Rule(
                id="DOC-01",
                name="Police report on file for a theft",
                applies_when="claim.loss_type == 'theft'",
                check="has(claim.police_report_number)",
            ),
            Rule(
                id="FRD-01",
                name="Reported within 30 days of the loss",
                severity="warn",
                check="days_between(claim.loss_date, claim.reported_date) <= 30",
                message="Reported {{ days_between(claim.loss_date, claim.reported_date) }} days after the loss",
            ),
            Rule(
                id="FRD-02",
                name="Third claim in twelve months",
                severity="info",
                applies_when="history.claims_last_12_months >= 2",
                check="false",
                message=(
                    "{{ history.claims_last_12_months + 1 }} claims in twelve months;"
                    " refer to the special investigations unit"
                ),
            ),
        ],
    )


POLICY = {
    "effective_from": "2026-01-01",
    "effective_until": "2026-12-31",
    "deductible": 500,
    "coverages": ["collision", "theft", "glass"],
    "limits": {"collision": 25000, "theft": 30000, "glass": 1500},
}


def test_a_covered_collision_pays_the_estimate_less_the_deductible():
    output = run(
        claim_adjudication(),
        {
            "claim": {
                "loss_type": "collision",
                "loss_date": "2026-08-02",
                "reported_date": "2026-08-04",
                "estimate": 6200,
            },
            "policy": POLICY,
            "history": {"claims_last_12_months": 0},
        },
    )

    assert output["status"] == "pass"
    assert output["derived"] == {"payable": 5700}
    assert statuses(output)["FRD-02"] == "not_applicable"


def test_a_late_theft_claim_without_a_police_report_is_held_with_every_reason():
    output = run(
        claim_adjudication(),
        {
            "claim": {
                "loss_type": "theft",
                "loss_date": "2026-06-01",
                "reported_date": "2026-07-20",
                "estimate": 41000,
            },
            "policy": POLICY,
            "history": {"claims_last_12_months": 2},
        },
    )

    assert statuses(output) == {
        "POL-01": "pass",
        "COV-01": "pass",
        "COV-02": "warn",
        "DOC-01": "fail",
        "FRD-01": "warn",
        "FRD-02": "info",
    }
    assert by_id(output)["COV-02"]["message"] == "Estimate 41000 is above the theft limit of 30000"
    assert by_id(output)["FRD-01"]["message"] == "Reported 49 days after the loss"
    assert by_id(output)["FRD-02"]["message"].startswith("3 claims in twelve months")


def test_an_uncovered_loss_fails_the_coverage_rule_and_holds_the_limit_check():
    output = run(
        claim_adjudication(),
        {
            "claim": {"loss_type": "flood", "loss_date": "2026-08-02", "reported_date": "2026-08-03", "estimate": 9000},
            "policy": POLICY,
            "history": {"claims_last_12_months": 0},
        },
    )

    # No flood coverage: the coverage rule fails, and the limit lookup finds nothing to compare with.
    assert statuses(output)["COV-01"] == "fail"
    assert statuses(output)["COV-02"] == "not_evaluated"
    assert output["status"] == "fail"


# --- Telecom: a port-out request checked for account takeover before it goes through ----------------------


def port_out_workflow() -> Workflow:
    start = Input(id="start", name="start")
    checks = Rules(
        id="takeover",
        name="takeover",
        input_fields=[NamedField(name="request"), NamedField(name="account"), NamedField(name="watchlist")],
        on_missing="fail",
        rules=[
            Rule(
                id="SEC-01",
                name="Account PIN verified on this request",
                check="request.pin_verified",
                reason_code="PIN",
            ),
            Rule(
                id="SEC-02",
                name="No SIM change in the last three days",
                applies_when="has(account.sim_changed_at)",
                check="days_between(account.sim_changed_at, request.requested_at) >= 3",
                message="SIM changed {{ days_between(account.sim_changed_at, request.requested_at) }} day(s) ago",
                reason_code="RECENT-SIM",
            ),
            Rule(
                id="SEC-03",
                name="Contact email unchanged for a week",
                severity="warn",
                applies_when="has(account.email_changed_at)",
                check="days_between(account.email_changed_at, request.requested_at) >= 7",
                reason_code="RECENT-EMAIL",
            ),
            Rule(
                id="SEC-04",
                name="Account older than 30 days for a port-out",
                applies_when="request.type == 'port_out'",
                check="days_between(account.opened_at, request.requested_at) >= 30",
                reason_code="NEW-ACCOUNT",
            ),
            Rule(
                id="SEC-05",
                name="Destination carrier not on the watch list",
                severity="warn",
                applies_when="request.type == 'port_out'",
                check="request.destination_carrier not in watchlist.carriers",
                reason_code="CARRIER",
            ),
        ],
        depends=[NodeDependency(node=start)],
        input_transformer=InputTransformer(
            selector={
                "request": "$.start.output.request",
                "account": "$.start.output.account",
                "watchlist": "$.start.output.watchlist",
            }
        ),
    )
    decision = Expression(
        id="decision",
        name="decision",
        expressions=[
            ExpressionItem(key="decision", expression="'approve' if status == 'pass' else 'hold'"),
            ExpressionItem(
                key="reasons",
                expression=(
                    "findings | selectattr('status', 'in', ['fail', 'warn'])" " | map(attribute='reason_code') | list"
                ),
            ),
        ],
        depends=[NodeDependency(node=checks)],
        input_transformer=InputTransformer(
            selector={"status": "$.takeover.output.status", "findings": "$.takeover.output.findings"}
        ),
    )
    end = Output(
        id="end",
        name="end",
        depends=[NodeDependency(node=decision)],
        input_transformer=InputTransformer(
            selector={"decision": "$.decision.output.decision", "reasons": "$.decision.output.reasons"}
        ),
    )
    return Workflow(id="port-out", flow=Flow(id="port-out-flow", nodes=[start, checks, decision, end]))


def test_a_port_out_after_a_sim_swap_is_held_and_a_clean_one_approved():
    watchlist = {"carriers": ["QuickPort Mobile"]}
    takeover = {
        "request": {
            "type": "port_out",
            "requested_at": "2026-09-19",
            "pin_verified": True,
            "destination_carrier": "QuickPort Mobile",
        },
        "account": {"opened_at": "2024-03-10", "sim_changed_at": "2026-09-18", "email_changed_at": "2026-09-17"},
        "watchlist": watchlist,
    }
    routine = {
        "request": {
            "type": "port_out",
            "requested_at": "2026-09-19",
            "pin_verified": True,
            "destination_carrier": "Northern Cellular",
        },
        "account": {"opened_at": "2021-06-01"},
        "watchlist": watchlist,
    }

    held = port_out_workflow().run(input_data=takeover, config=RunnableConfig(callbacks=[]))
    approved = port_out_workflow().run(input_data=routine, config=RunnableConfig(callbacks=[]))

    assert held.output["end"]["output"] == {"decision": "hold", "reasons": ["RECENT-SIM", "RECENT-EMAIL", "CARRIER"]}
    assert approved.output["end"]["output"] == {"decision": "approve", "reasons": []}


def test_a_request_without_the_pin_answer_is_refused_under_the_strict_policy():
    result = port_out_workflow().run(
        input_data={
            "request": {"type": "sim_swap", "requested_at": "2026-09-19"},
            "account": {"opened_at": "2025-01-01"},
            "watchlist": {"carriers": []},
        },
        config=RunnableConfig(callbacks=[]),
    )

    findings = by_id(result.output["takeover"]["output"])
    assert findings["SEC-01"]["status"] == "fail"
    assert findings["SEC-01"]["message"] == "missing value for request.pin_verified"
    assert result.output["end"]["output"]["decision"] == "hold"


# --- Healthcare: a medication order checked against the formulary and the patient -------------------------


def order_checks() -> Rules:
    return Rules(
        id="pharmacy",
        name="pharmacy",
        input_fields=[NamedField(name="order"), NamedField(name="patient"), NamedField(name="formulary")],
        derived_values=[
            DerivedValue(name="entry", expression="formulary[order.drug]"),
            DerivedValue(name="daily_mg", expression="order.dose_mg * order.doses_per_day"),
            DerivedValue(name="mg_per_kg", expression="order.dose_mg / patient.weight_kg"),
        ],
        rules=[
            Rule(
                id="MED-01",
                name="Daily dose within the formulary maximum",
                check="daily_mg <= entry.max_daily_mg",
                message="{{ daily_mg }} mg/day ordered, {{ entry.max_daily_mg }} mg/day maximum",
            ),
            Rule(
                id="MED-02",
                name="Weight-based dose within the paediatric limit",
                applies_when="patient.age < 18",
                check="mg_per_kg <= entry.mg_per_kg_max",
                message="{{ mg_per_kg | round(2) }} mg/kg per dose, {{ entry.mg_per_kg_max }} mg/kg maximum",
            ),
            Rule(
                id="MED-03",
                name="No recorded allergy to the drug class",
                check="entry.allergy_class not in patient.allergies",
                message="Patient allergic to {{ entry.allergy_class }}",
            ),
            Rule(
                id="MED-04",
                name="Renal adjustment recorded for reduced kidney function",
                severity="warn",
                applies_when="patient.egfr < entry.renal_threshold_egfr",
                check="has(order.renal_adjustment)",
            ),
        ],
    )


FORMULARY = {
    "amoxicillin": {
        "max_daily_mg": 3000,
        "mg_per_kg_max": 45,
        "allergy_class": "penicillin",
        "renal_threshold_egfr": 30,
    },
    "metformin": {"max_daily_mg": 2550, "mg_per_kg_max": 30, "allergy_class": "biguanide", "renal_threshold_egfr": 45},
}


def test_a_paediatric_order_over_the_weight_limit_and_an_allergy_are_both_found():
    child = run(
        order_checks(),
        {
            "order": {"drug": "amoxicillin", "dose_mg": 800, "doses_per_day": 3},
            "patient": {"age": 6, "weight_kg": 16, "allergies": [], "egfr": 110},
            "formulary": FORMULARY,
        },
    )
    allergic = run(
        order_checks(),
        {
            "order": {"drug": "amoxicillin", "dose_mg": 500, "doses_per_day": 3},
            "patient": {"age": 41, "weight_kg": 80, "allergies": ["penicillin"], "egfr": 95},
            "formulary": FORMULARY,
        },
    )

    assert statuses(child) == {"MED-01": "pass", "MED-02": "fail", "MED-03": "pass", "MED-04": "not_applicable"}
    assert by_id(child)["MED-02"]["message"] == "50.0 mg/kg per dose, 45 mg/kg maximum"
    assert statuses(allergic)["MED-03"] == "fail" and statuses(allergic)["MED-02"] == "not_applicable"


def test_reduced_kidney_function_without_an_adjustment_is_a_warning():
    output = run(
        order_checks(),
        {
            "order": {"drug": "metformin", "dose_mg": 500, "doses_per_day": 2},
            "patient": {"age": 70, "weight_kg": 72, "allergies": [], "egfr": 38},
            "formulary": FORMULARY,
        },
    )

    assert statuses(output) == {"MED-01": "pass", "MED-02": "not_applicable", "MED-03": "pass", "MED-04": "warn"}
    assert output["status"] == "warn"


def test_a_drug_missing_from_the_formulary_holds_every_rule_that_needs_its_entry():
    output = run(
        order_checks(),
        {
            "order": {"drug": "novadrug", "dose_mg": 10, "doses_per_day": 1},
            "patient": {"age": 30, "weight_kg": 70, "allergies": [], "egfr": 100},
            "formulary": FORMULARY,
        },
    )

    assert output["derived"]["entry"] is None
    assert statuses(output) == {
        "MED-01": "not_evaluated",
        "MED-02": "not_applicable",
        "MED-03": "not_evaluated",
        "MED-04": "not_evaluated",
    }
    assert by_id(output)["MED-01"]["message"] == "missing value for entry.max_daily_mg"
    assert output["status"] == "not_evaluated"


# --- Government: a housing assistance application screened against the programme rules -------------------


def housing_screening() -> Rules:
    return Rules(
        id="screening",
        name="screening",
        input_fields=[NamedField(name="household"), NamedField(name="limits"), NamedField(name="as_of")],
        rules=[
            Rule(
                id="ELG-01",
                name="Income at or below the limit for the household size",
                check="household.monthly_income <= limits[household.state].by_size[household.size - 1]",
                message=(
                    "Income {{ household.monthly_income }} is above the limit of"
                    " {{ limits[household.state].by_size[household.size - 1] }}"
                    " for {{ household.size }} people"
                ),
            ),
            Rule(
                id="ELG-02", name="State residency of at least twelve months", check="household.residency_months >= 12"
            ),
            Rule(
                id="ELG-03",
                name="A dependant under 18 or a member over 62 in the household",
                check=(
                    "(household.members | selectattr('age', 'lt', 18) | list | length) > 0"
                    " or (household.members | selectattr('age', 'gt', 62) | list | length) > 0"
                ),
            ),
            Rule(
                id="DOC-01",
                name="Identity document on file for every adult",
                check=(
                    "(household.members | selectattr('age', 'ge', 18)"
                    " | rejectattr('id_on_file') | list | length) == 0"
                ),
            ),
            Rule(
                id="AST-01",
                name="Assets under the programme ceiling",
                check="household.assets <= 5000",
                effective_from="2027-01-01",
                references=["Programme rule change, 2027 plan year"],
            ),
        ],
    )


LIMITS = {"CA": {"by_size": [2900, 3300, 3700, 4100]}, "TX": {"by_size": [2200, 2500, 2800, 3100]}}


def test_a_family_qualifies_this_year_and_meets_the_asset_test_only_from_next_year():
    household = {
        "state": "CA",
        "size": 3,
        "monthly_income": 3500,
        "residency_months": 30,
        "assets": 7200,
        "members": [{"age": 34, "id_on_file": True}, {"age": 31, "id_on_file": True}, {"age": 4, "id_on_file": False}],
    }

    this_year = run(housing_screening(), {"household": household, "limits": LIMITS, "as_of": "2026-09-19"})
    next_year = run(housing_screening(), {"household": household, "limits": LIMITS, "as_of": "2027-02-01"})

    assert this_year["status"] == "pass"
    assert statuses(this_year)["AST-01"] == "not_applicable"
    assert statuses(next_year)["AST-01"] == "fail"


def test_an_application_over_the_limit_with_a_missing_document_says_both():
    output = run(
        housing_screening(),
        {
            "household": {
                "state": "TX",
                "size": 2,
                "monthly_income": 2700,
                "residency_months": 8,
                "assets": 100,
                "members": [{"age": 45, "id_on_file": True}, {"age": 44, "id_on_file": False}],
            },
            "limits": LIMITS,
            "as_of": "2026-09-19",
        },
    )

    assert statuses(output) == {
        "ELG-01": "fail",
        "ELG-02": "fail",
        "ELG-03": "fail",
        "DOC-01": "fail",
        "AST-01": "not_applicable",
    }
    assert by_id(output)["ELG-01"]["message"] == "Income 2700 is above the limit of 2500 for 2 people"


# --- Manufacturing: a batch released only when every measured attribute is in specification --------------


def release_workflow() -> Workflow:
    start = Input(id="start", name="start")
    attribute_checks = Rules(
        id="attribute",
        name="attribute",
        input_fields=[
            NamedField(name="name"),
            NamedField(name="value"),
            NamedField(name="spec"),
            NamedField(name="method_validated"),
        ],
        derived_values=[DerivedValue(name="attribute", expression="name")],
        rules=[
            Rule(
                id="QC-01",
                name="Result within specification",
                check="spec.min <= value and value <= spec.max",
                message="{{ name }} measured {{ value }}, specification {{ spec.min }} to {{ spec.max }}",
            ),
            Rule(id="QC-02", name="Test method validated", severity="warn", check="method_validated"),
        ],
    )
    release = Map(
        id="release",
        name="release",
        node=attribute_checks,
        max_workers=4,
        depends=[NodeDependency(node=start)],
        input_transformer=InputTransformer(selector={"input": "$.start.output.batch.attributes"}),
    )
    verdict = Expression(
        id="verdict",
        name="verdict",
        expressions=[
            ExpressionItem(
                key="released", expression="(results | selectattr('status', 'equalto', 'fail') | list | length) == 0"
            ),
            ExpressionItem(
                key="held_on",
                expression="results | selectattr('status', 'ne', 'pass') | map(attribute='derived.attribute') | list",
            ),
        ],
        depends=[NodeDependency(node=release)],
        input_transformer=InputTransformer(selector={"results": "$.release.output.output"}),
    )
    end = Output(
        id="end",
        name="end",
        depends=[NodeDependency(node=verdict)],
        input_transformer=InputTransformer(
            selector={"released": "$.verdict.output.released", "held_on": "$.verdict.output.held_on"}
        ),
    )
    return Workflow(id="release", flow=Flow(id="release-flow", nodes=[start, release, verdict, end]))


BATCH = {
    "id": "LOT-2026-0917",
    "attributes": [
        {"name": "assay", "value": 99.1, "spec": {"min": 98.0, "max": 102.0}, "method_validated": True},
        {"name": "moisture", "value": 0.9, "spec": {"min": 0.0, "max": 0.5}, "method_validated": True},
        {"name": "particle_size", "value": 42, "spec": {"min": 30, "max": 60}, "method_validated": False},
    ],
}


def test_a_batch_with_a_result_out_of_specification_is_held_on_that_attribute():
    result = release_workflow().run(input_data={"batch": BATCH}, config=RunnableConfig(callbacks=[]))

    assert result.status == RunnableStatus.SUCCESS
    assert result.output["end"]["output"] == {"released": False, "held_on": ["moisture", "particle_size"]}
    per_attribute = result.output["release"]["output"]["output"]
    assert [item["status"] for item in per_attribute] == ["pass", "fail", "warn"]
    assert by_id(per_attribute[1])["QC-01"]["message"] == "moisture measured 0.9, specification 0.0 to 0.5"


def test_the_release_workflow_round_trips_through_yaml_with_the_rules_inside_the_map(tmp_path):
    path = tmp_path / "release.yaml"
    release_workflow().to_yaml_file(path)

    loaded = Workflow.from_yaml_file(str(path), init_components=True)
    inner = next(node for node in loaded.flow.nodes if isinstance(node, Map)).node
    result = loaded.run(input_data={"batch": BATCH}, config=RunnableConfig(callbacks=[]))

    assert isinstance(inner, Rules) and [rule.id for rule in inner.rules] == ["QC-01", "QC-02"]
    assert result.output["end"]["output"] == {"released": False, "held_on": ["moisture", "particle_size"]}


# --- The node under pressure: an escape attempt, five hundred rules, a batch across workers ---------------


def test_a_rule_that_reaches_for_python_internals_is_refused_when_the_node_is_built():
    with pytest.raises(ValueError, match=r"rule 1 \(escape\): the check reads a private attribute"):
        Rules(
            id="x",
            name="x",
            input_fields=[NamedField(name="record")],
            rules=[Rule(id="r", name="escape", check="record.__class__.__mro__[1].__subclasses__() | length > 0")],
        )


def test_an_escape_through_a_filter_fails_the_run_instead_of_running():
    node = Rules(
        id="x",
        name="x",
        input_fields=[NamedField(name="record")],
        rules=[Rule(id="r", name="escape", check="(record | attr('__class__')) == 'dict'")],
    )

    result = node.run(input_data={"record": {"a": 1}}, config=RunnableConfig(callbacks=[]))

    assert result.status == RunnableStatus.FAILURE
    assert "unsafe" in str(result.error).lower()


def test_five_hundred_rules_cost_milliseconds_per_record_and_a_batch_runs_per_record_under_a_map():
    rules = [
        Rule(
            id=f"CHK-{index:03d}",
            name=f"Reading {index} within limit",
            check=f"record.readings[{index % 20}] <= limits.max",
            message="Reading {{ record.readings[" + str(index % 20) + "] }} above {{ limits.max }}",
        )
        for index in range(500)
    ]
    node = Rules(
        id="sensors", name="sensors", input_fields=[NamedField(name="record"), NamedField(name="limits")], rules=rules
    )
    record = {"record": {"readings": [float(i) for i in range(20)]}, "limits": {"max": 10}}

    run(node, record)
    started = time.perf_counter()
    output = run(node, record)
    per_record = time.perf_counter() - started

    assert len(output["findings"]) == 500 and output["summary"]["fail"] == 500 * 9 // 20
    assert len(node.to_dict(for_tracing=True)["rules"]) == 50
    # A few milliseconds locally; the bound only catches a gross regression, such as compiling per run,
    # without tying the suite to the speed of the machine it runs on.
    assert per_record < 2, f"500 rules took {per_record:.3f}s for one record"

    batch = Map(id="batch", name="batch", node=node, max_workers=8)
    started = time.perf_counter()
    result = batch.run(input_data={"input": [record] * 16}, config=RunnableConfig(callbacks=[]))
    elapsed = time.perf_counter() - started

    assert result.status == RunnableStatus.SUCCESS
    assert [item["summary"]["fail"] for item in result.output["output"]] == [500 * 9 // 20] * 16
    # The batch is bounded by the serial cost measured on this machine, so it never depends on core count.
    assert elapsed < max(5.0, per_record * 16 * 3), f"16 records took {elapsed:.1f}s, one took {per_record:.3f}s"
