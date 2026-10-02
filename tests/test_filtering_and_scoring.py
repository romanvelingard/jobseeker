import unittest
import os
import yaml

from agent import is_valid_role_title, step3_filter_exclusions, parse_job_entries
from scorer import calculate_rule_score

class TestJobFilteringAndScoring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open("profiles/qa_director.yaml", "r", encoding="utf-8") as f:
            cls.qa_config = yaml.safe_load(f)
        with open("profiles/procurement.yaml", "r", encoding="utf-8") as f:
            cls.proc_config = yaml.safe_load(f)

    def test_qa_director_offending_titles_rejected(self):
        offending_titles = [
            "Mechanical Engineering Manager",
            "DevOps Manager",
            "Software Development Team Leader",
            "Mechanical R&D Manager – Medical Devices",
            "Software Manager, DOCA Services"
        ]
        for title in offending_titles:
            with self.subTest(title=title):
                self.assertFalse(
                    is_valid_role_title(title, self.qa_config),
                    f"Title '{title}' should be marked invalid by is_valid_role_title"
                )
                jobs_list = [{"title": title, "desc": "Leading team", "location": "Israel"}]
                filtered = step3_filter_exclusions(jobs_list, self.qa_config.get("exclude", []), self.qa_config)
                self.assertEqual(
                    len(filtered), 0,
                    f"Title '{title}' should be excluded by step3_filter_exclusions"
                )

    def test_qa_director_valid_titles_accepted(self):
        valid_titles = [
            "QA Director",
            "Director of Quality Assurance",
            "Director of QA",
            "Head of QA",
            "Director of Test Engineering",
            "Director of Test Automation",
            "Quality Engineering Director",
            "VP Quality Assurance",
            "QA Senior Manager",
            "Senior QA Manager",
            "QA Engineering Manager",
            "Software Test Manager",
            "QA Automation Manager",
            "דירקטור QA",
            "מנהל בדיקות תוכנה",
            "מנהל איכות"
        ]
        for title in valid_titles:
            with self.subTest(title=title):
                self.assertTrue(
                    is_valid_role_title(title, self.qa_config),
                    f"Title '{title}' should be valid for QA profile"
                )
                jobs_list = [{"title": title, "desc": "QA leadership", "location": "Israel"}]
                filtered = step3_filter_exclusions(jobs_list, self.qa_config.get("exclude", []), self.qa_config)
                self.assertEqual(
                    len(filtered), 1,
                    f"Title '{title}' should pass step3_filter_exclusions"
                )

    def test_procurement_valid_titles_accepted(self):
        valid_titles = [
            "Procurement Specialist",
            "Purchasing Specialist",
            "Strategic Sourcing Specialist",
            "Senior Buyer",
            "Supply Chain Specialist",
            "קניין רכש"
        ]
        for title in valid_titles:
            with self.subTest(title=title):
                self.assertTrue(
                    is_valid_role_title(title, self.proc_config),
                    f"Title '{title}' should be valid for Procurement profile"
                )
                jobs_list = [{"title": title, "desc": "Procurement operations", "location": "Israel"}]
                filtered = step3_filter_exclusions(jobs_list, self.proc_config.get("exclude", []), self.proc_config)
                self.assertEqual(
                    len(filtered), 1,
                    f"Title '{title}' should pass step3_filter_exclusions"
                )

    def test_profile_aware_scoring(self):
        qa_job = {"title": "QA Director", "country": "Israel", "desc": "Medical Devices high tech company"}
        qa_score = calculate_rule_score(qa_job, self.qa_config)
        self.assertGreaterEqual(qa_score, 80.0, "QA Director position in Israel should get high rule score")

        proc_job = {"title": "Senior Buyer", "country": "Israel", "desc": "Defense supplier management"}
        proc_score = calculate_rule_score(proc_job, self.proc_config)
        self.assertGreaterEqual(proc_score, 80.0, "Senior Buyer position in Israel should get high rule score")

if __name__ == "__main__":
    unittest.main()
