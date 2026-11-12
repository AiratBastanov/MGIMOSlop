"""Новый all-periods selector: оценки из прежнего периода не теряются."""
from datetime import date, datetime, timezone
from django.test import SimpleTestCase
from studies.academic_selectors import study_view, DOMAINS


class AllResultsTests(SimpleTestCase):
    def test_all_assessed_includes_previous_period_and_negative_grade_with_exact_requirement(self):
        now = datetime(2026,11,8,12,tzinfo=timezone.utc)
        periods = [
            {'id':1,'source_id':'previous','name':'Прошлый семестр','date_from':date(2025,9,1),'date_until':date(2026,2,1)},
            {'id':2,'source_id':'current','name':'Текущий семестр','date_from':date(2026,9,1),'date_until':date(2027,2,1)}]
        requirement = {'reason':'Дополнить расчёт','due_exact':'10.11.2026, 18:00 МСК'}
        courses = [
            {'id':1,'period_id':1,'result':{'label':'5'},'classification':'positive','has_open':False,'open_requirements':[],'overdue_count':0},
            {'id':2,'period_id':2,'result':{'label':'2'},'classification':'open','has_open':True,'open_requirements':[requirement],'overdue_count':0},
            {'id':3,'period_id':2,'result':None,'classification':'unentered','has_open':False,'open_requirements':[],'overdue_count':0}]
        snapshot = {'now':now,'periods':periods,'source':{'state':'ready','message':''},'courses':courses,'assessments':[],
            'coverage':[{'period_id':period['id'],'entity_type':kind,'completeness':'complete'} for period in periods for kind in DOMAINS]}
        current = study_view(snapshot,filter_name='assessed')
        self.assertEqual([row['id'] for row in current['courses']], [2])
        all_results = study_view(snapshot,period_id='all',filter_name='assessed')
        self.assertEqual([row['id'] for row in all_results['courses']], [1,2])
        self.assertEqual([row['period_name'] for row in all_results['courses']], ['Прошлый семестр','Текущий семестр'])
        self.assertEqual(all_results['courses'][1]['open_requirements'], [requirement])
        self.assertEqual(all_results['summary']['N'],3)
        self.assertEqual(all_results['filtered_count'],2)
