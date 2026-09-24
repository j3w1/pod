import unittest

from pod.context import bind_context, context_valid, execution_brief, feedback, steer
from pod.errors import PodError


class ContextTests(unittest.TestCase):
    def test_binding_and_steering_invalidate_affected_work(self):
        bound=bind_context(candidate='c',instructions=[],requirements='r',sources=[],policy_revision='p',summary='small')
        self.assertTrue(context_valid(bound,bound['binding']))
        self.assertFalse(context_valid(bound,{**bound['binding'],'candidate':'new'}))
        plan={'schema':'pod-plan/v1','revision':1,'criteria':['works'],'assignments':['a','b'],'candidate':'c'}
        changed=steer(plan,changes={'assignments':['b'],'reason':'a no longer needed'},authorized_acceptance_change=False)
        self.assertEqual(changed['affected'],['a'])
        self.assertEqual(changed['plan']['revision'],2)
        with self.assertRaises(PodError):
            steer(plan,changes={'criteria':['different'],'reason':'scope changed'},authorized_acceptance_change=False)

    def test_brief_keeps_every_criterion(self):
        brief=execution_brief(['a','b'],[{'criterion':'a','check':'unit'},{'criterion':'b','dependency':'owner'}])
        self.assertEqual(brief['criteria'],['a','b'])
        with self.assertRaises(PodError):
            execution_brief(['a','b'],[{'criterion':'a','check':'unit'}])

    def test_feedback_never_writes_or_infers_savings(self):
        observed=[{'route':'gpt-6-sol','outcome':'blocked'}]*3
        self.assertFalse(feedback(observed)['writes'])
        self.assertIsNone(feedback(observed[:2])['suggestion'])
