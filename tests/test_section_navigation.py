from django.test import SimpleTestCase
from django.urls import resolve
from accounts.navigation import navigation
from discovery.catalog import actions, parse_query, ranked
from studies.routes import ALL_ASSESSED_RESULTS_URL


class SectionNavigationTests(SimpleTestCase):
    def results(self,role,value):
        query,tokens,_=parse_query(value)
        return ranked(actions(role),query,tokens)

    def test_visible_destinations_resolve_and_are_searchable_once(self):
        for role in ('student','teacher','staff'):
            links=navigation(role)
            rows=actions(role)
            self.assertEqual(len(rows),len({row['url'] for row in rows}))
            for link in (*links['primary'],*links['profile']):
                resolve(link['url'])
                with self.subTest(role=role,label=link['label']):
                    self.assertIn(link['url'],[row['url'] for row in self.results(role,link['label'])])

    def test_schedule_variants_and_authorized_roles(self):
        for role,url in [('student','/schedule/'),('teacher','/teacher/schedule/')]:
            for query in ('Расписание','РАСПИСАНИЕ ЗАНЯТИЙ','моё расписание','мое расписание','пары','распис','расписание зан'):
                with self.subTest(role=role,query=query):
                    self.assertEqual(self.results(role,query)[0]['url'],url)
        self.assertEqual(self.results('staff','расписание'),[])
        for role in ('',None,'anonymous','admin'):
            self.assertEqual(actions(role),[])
            self.assertEqual(navigation(role)['primary'],())

    def test_exact_sections_grades_and_unrelated_query(self):
        self.assertEqual(self.results('student','УЧЁБА')[0]['url'],'/study/')
        self.assertEqual(self.results('student','Настройки профиля')[0]['url'],'/profile/edit/')
        self.assertEqual(self.results('student','оценки')[0]['url'],ALL_ASSESSED_RESULTS_URL)
        self.assertEqual(self.results('student','космодромxyz'),[])
