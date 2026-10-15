"""Automated 1,000+ Iteration Chaos, Edge-Case, Happy-Path & Error-Contract Test Suite (hermetic)."""

from __future__ import annotations

import unittest

from scripts import stress_loop_1000


class TestChaos1000Loop(unittest.TestCase):
  """Executes at least 1,000 iterations across happy paths, edge cases, user errors, live guards, and code errors."""

  def test_1050_iteration_full_loop_zero_failures(self) -> None:
    report = stress_loop_1000.run_stress_loop(total_iterations=1050, seed=42)
    self.assertGreaterEqual(report['total_iterations'], 1000)
    self.assertEqual(report['total_failed'], 0, f"Failures: {report['failures']}")
    self.assertEqual(report['total_passed'], report['total_iterations'])

    # The loop audits itself: no network attempt may happen, and no fuzz slot may complete a cycle with gaps.
    self.assertEqual(report['hermetic']['network_attempts'], 0, report['hermetic'])
    self.assertEqual(report['fuzz_coverage_gaps'], [])
    for slot in ('b2.recompute_finops.w_val', 'b2.recompute_finops.w_val2', 'b2.what_if_simulate.w_val',
                 'b2.prompt_xray.w_val', 'b2.state_query.w_val', 'b2.ge_mart_refresh.w_val',
                 'p2.prompt_xray.monthly_requests', 'p4.adk.weird'):
      cov = report['fuzz_coverage'][slot]
      self.assertEqual(cov['distinct'], cov['of'], f'{slot} did not see every edge value: {cov}')

    # Verify every backend & frontend category was exercised thoroughly
    b_counts = report['backend_category_counts']
    self.assertGreaterEqual(b_counts['happy_path'], 150)
    self.assertGreaterEqual(b_counts['edge_fuzz'], 150)
    self.assertGreaterEqual(b_counts['validation_error'], 150)
    self.assertGreaterEqual(b_counts['live_mode_guardrail'], 150)
    self.assertGreaterEqual(b_counts['internal_code_error'], 150)

    f_counts = report['frontend_category_counts']
    self.assertGreaterEqual(f_counts['ui_happy'], 40)
    self.assertGreaterEqual(f_counts['ui_edge_fuzz'], 40)
    self.assertGreaterEqual(f_counts['ui_validation_err'], 40)
    self.assertGreaterEqual(f_counts['ui_live_guard'], 40)
    self.assertGreaterEqual(f_counts['ui_code_bug'], 40)
    self.assertGreaterEqual(report['ghost_dom_real_renderer_checks'], 60)

    # Verify all 10 deep architectural verification pillars were exercised
    p_counts = report['deep_pillar_counts']
    self.assertGreaterEqual(p_counts['p1_finops_math_conservation'], 80)
    self.assertGreaterEqual(p_counts['p2_prompt_xray_properties'], 80)
    self.assertGreaterEqual(p_counts['p3_mcp_protocol_and_tools'], 80)
    self.assertGreaterEqual(p_counts['p4_adk_agent_tools'], 80)
    self.assertGreaterEqual(p_counts['p5_nl2sql_ast_and_safety'], 80)
    self.assertGreaterEqual(p_counts['p6_ge_mart_and_billing_join'], 80)
    self.assertGreaterEqual(p_counts['p7_grounding_and_sme_rubric'], 80)
    self.assertGreaterEqual(p_counts['p8_concurrency_thread_safety'], 80)
    self.assertGreaterEqual(p_counts['p9_node_ui_deep_fuzz'], 200)
    self.assertGreaterEqual(p_counts['p10_full_stack_fault_injection'], 150)

    # The promises that used to be unverified are now counted.
    extra = report['extra_counts']
    self.assertEqual(extra['log_correlation_verified'], b_counts['internal_code_error'])
    self.assertGreaterEqual(extra['live_policy_guarded_checks'], 20)
    self.assertGreaterEqual(extra['live_policy_allowed_checks'], 40)
    self.assertGreaterEqual(extra['mcp_http_route_checks'], 16)
    self.assertGreaterEqual(extra['concurrency_http_requests'], 256)
    self.assertGreaterEqual(extra['missing_field_validation_checks'], 30)
    self.assertGreaterEqual(extra['bigquery_error_translation_checks'], 30)
    self.assertGreaterEqual(extra['llm_judge_non_override_checks'], 20)
    self.assertGreaterEqual(len(report['live_policy_post_routes']), 20)


if __name__ == '__main__':
  unittest.main()
