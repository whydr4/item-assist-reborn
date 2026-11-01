import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('log_report', Path(__file__).resolve().parents[1] / 'research/log_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

class ReportRegression(unittest.TestCase):
    def test_old_log_uses_pre_purchase_advice(self):
        row = lambda i: [i, 1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0]
        events = [
            {'ev':'recs','t':10,'owned':[],'top':[row(1)],'hero':35},
            {'ev':'recs','t':11,'owned':[1],'top':[row(2)],'hero':35},
            {'ev':'buy','t':11,'item':1,'rank':-1,'byScore':-1},
        ]
        out=[]
        report.report_match({'events':events,'hero':35},{1:'Base',2:'Other'},{},{},out)
        self.assertIn('в топ-1 1/1', '\n'.join(out))

    def test_unknown_snapshot_is_excluded(self):
        out=[]
        report.report_match({'events':[{'ev':'buy','t':1,'item':1,'rank':None,'adviceHb':None}],'hero':35},{1:'Base'},{},{},out)
        self.assertIn('исключены из долей', '\n'.join(out))
        self.assertNotIn('Совпадение с советом', '\n'.join(out))

if __name__ == '__main__':
    unittest.main()
