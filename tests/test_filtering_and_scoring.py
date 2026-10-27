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

    def test_extract_skills_summary_never_returns_none(self):
        from scorer import extract_skills_summary

        bad_descriptions = ["None", "none", "nan", "NaN", "null", "", None, "   "]
        for bad_desc in bad_descriptions:
            with self.subTest(bad_desc=bad_desc):
                summary = extract_skills_summary(bad_desc, "Procurement Specialist", ["Procurement", "Sourcing"], ["Defense"])
                self.assertNotIn("Overview: None", summary, "Summary must never output 'Overview: None'")
                self.assertNotIn("— None", summary, "Summary must never output '— None'")
                self.assertTrue(len(summary) > 0, "Summary must not be empty")

        # Test with real description
        real_desc = "DriveNets is looking for a Procurement Specialist with 5 years experience in hardware sourcing."
        summary_real = extract_skills_summary(real_desc, "Procurement Specialist", ["Procurement", "Sourcing"], ["Networking"])
        self.assertIn("Key Skills/Industry", summary_real)
        self.assertIn("DriveNets is looking for", summary_real)

    def test_extract_linkedin_job_id(self):
        from agent import extract_linkedin_job_id

        self.assertEqual(extract_linkedin_job_id("https://www.linkedin.com/jobs/view/4479185173"), "4479185173")
        self.assertEqual(extract_linkedin_job_id("https://il.linkedin.com/jobs/view/4475814553?refId=123"), "4475814553")
        self.assertEqual(extract_linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=4474243974&geoId=101620261"), "4474243974")
        self.assertIsNone(extract_linkedin_job_id("https://www.indeed.com/viewjob?jk=abcdef123456"))

if __name__ == "__main__":
    unittest.main()

