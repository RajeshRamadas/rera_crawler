"""
Unit Tests for Karnataka Property Legal Risk & Due-Diligence Engine
====================================================================
"""

import sys
import unittest
from legal_risk_engine import KarnatakaLegalRiskEngine, CheckStatus, BuyGateDecision
from legal_due_diligence_agent import KarnatakaPropertyLegalAgent, SAMPLE_PROPERTIES


class TestKarnatakaLegalEngine(unittest.TestCase):

    def setUp(self):
        self.agent = KarnatakaPropertyLegalAgent()
        self.engine = KarnatakaLegalRiskEngine()

    def test_clear_land_plot_sarjapur(self):
        """Test clean Sarjapur land plot returns APPROVED TO BUY with high legal score."""
        payload = SAMPLE_PROPERTIES["sarjapur_plot"]
        result = self.agent.run_due_diligence(payload)

        self.assertEqual(result["buy_gate_decision"], BuyGateDecision.APPROVED_TO_BUY.value)
        self.assertGreater(result["overall_legal_score"], 90.0)
        self.assertEqual(len(result["critical_failures"]), 0)
        self.assertTrue(result["mandatory_gate_met"])

    def test_whitefield_apartment_with_bank_mortgage(self):
        """Test Whitefield apartment with Bank NOC returns APPROVED TO BUY or BUY WITH CAUTION."""
        payload = SAMPLE_PROPERTIES["whitefield_apartment"]
        result = self.agent.run_due_diligence(payload)

        self.assertIn(result["buy_gate_decision"], [BuyGateDecision.APPROVED_TO_BUY.value, BuyGateDecision.BUY_WITH_CAUTION.value])
        self.assertGreater(result["overall_legal_score"], 85.0)
        self.assertEqual(len(result["critical_failures"]), 0)

    def test_kengeri_illegal_plot_rejection(self):
        """Test Kengeri plot with PTCL, B-Kharab, & Rajakaluvé buffer breaches returns DO NOT BUY."""
        payload = SAMPLE_PROPERTIES["kengeri_illegal_plot"]
        result = self.agent.run_due_diligence(payload)

        self.assertEqual(result["buy_gate_decision"], BuyGateDecision.DO_NOT_BUY.value)
        self.assertLess(result["overall_legal_score"], 40.0)
        self.assertGreater(len(result["critical_failures"]), 3)
        self.assertFalse(result["mandatory_gate_met"])
        self.assertGreater(len(result["contradiction_log"]), 0)

    def test_all_26_checks_present(self):
        """Ensure all 26 matrix checks are evaluated in the output."""
        payload = SAMPLE_PROPERTIES["sarjapur_plot"]
        result = self.agent.run_due_diligence(payload)

        matrix_checks = result["matrix_checks"]
        self.assertEqual(len(matrix_checks), 26)

        # Check key Karnataka specific areas exist
        expected_areas = [
            "Seller identity", "Current title", "30+ year title chain", "Parent documents",
            "EC", "RTC", "Mutation", "Survey/Hissa", "Conversion", "PTCL/Grant",
            "Government land", "Acquisition", "Lake/Rajakaluvé", "Master plan",
            "Layout approval", "Khata/e-Khata", "Property tax", "Litigation",
            "Physical survey", "GIS verification", "RERA", "Building approval",
            "OC/CC", "UDS", "Mortgage/NOC", "Sale agreement"
        ]
        for area in expected_areas:
            self.assertIn(area, matrix_checks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
