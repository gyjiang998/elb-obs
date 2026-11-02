"""Feature -> calibration -> search integration in disposable numerical fixtures."""
import tempfile
import unittest
from pathlib import Path

from backend.core import PlatformError
from backend.parallel_modeling.manager import ParallelModelingManager, HARDWARE_ID
from backend.tests.test_parallel_interference import pair_measurements


def two_stage_trace():
    events = [(1.000, 'sched_switch', 'prev_comm=idle prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=app next_pid=10 next_prio=120'),
              (1.001, 'tracing_mark_write', 'B|10|H:UIVsyncTask[timestamp:1000000000][vsyncID:1][instanceID:1]'),
              (1.002, 'tracing_mark_write', 'B|10|H:CustomNode:BuildItem [First]'),
              (1.004, 'tracing_mark_write', 'E|10'),
              (1.004, 'tracing_mark_write', 'B|10|H:CustomNode:BuildItem [Second]'),
              (1.006, 'tracing_mark_write', 'E|10'),
              (1.007, 'tracing_mark_write', 'E|10'),
              (1.008, 'sched_switch', 'prev_comm=app prev_pid=10 prev_prio=120 prev_state=S ==> next_comm=idle next_pid=0 next_prio=120')]
    return ''.join(f'app-10 (   10) [000] .... {t:.6f}: {event}: {body}\n' for t, event, body in events)


class WorkloadModelingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ParallelModelingManager(Path(self.temp.name))
        self.dataset = self.manager.import_dataset({'trace_text': two_stage_trace(), 'hardware_profile_id': HARDWARE_ID})
        plan = self.manager.calibration.create_plan({'parameter_groups': ['pairwise_interference'], 'repeats': 3})
        fit = self.manager.calibration.fit({'plan_id': plan['id'], 'measurements': pair_measurements(plan)})
        self.assertTrue(fit['quality']['passed'], fit['quality'])
        self.version = self.manager.calibration.publish({'fit_id': fit['id']})

    def profile(self, kind):
        source = self.manager.workload_features.analyze({'dataset_id': self.dataset['id']})
        counters = {'instructions': 2000, 'cycles': 1000, 'llc_misses': 2} if kind == 'compute' else {'instructions': 1000, 'cycles': 2000, 'llc_misses': 40}
        measurements = {'schema_version': 'workload_stage_measurements/v1', 'dataset_id': self.dataset['id'],
                        'hardware_profile_id': HARDWARE_ID, 'clock_domain': 'trace_monotonic',
                        'clock_alignment_evidence': 'Isolated synthetic fixture uses trace clock',
                        'provenance': {'source': 'unit-test', 'run_id': kind, 'collector': 'test', 'data_kind': 'synthetic_fixture'},
                        'stages': [{'stage_id': stage['id'], 'start_ns': stage['start_ns'], 'end_ns': stage['end_ns'],
                                    'tid': 10, 'measurement_scope': 'thread_user',
                                    'counters': {key: {'value': value, 'unit': 'count', 'event': key} for key, value in counters.items()},
                                    'event_mapping': {'llc_misses': {'verified': True, 'cache_level': 'LLC', 'event': 'llc_misses'}}}
                                   for stage in source['stages']]}
        return self.manager.workload_features.analyze({'dataset_id': self.dataset['id'], 'measurements': measurements})

    def search(self, profile):
        analysis = self.manager.analyze({'dataset_id': self.dataset['id'], 'model_package_id': self.version['id'], 'feature_profile_id': profile['id']})
        result = self.manager.search({'analysis_id': analysis['id'], 'space': {'available_core_group_ids': ['big', 'middle'],
                                      'worker_counts': [2], 'main_participation': [False], 'frequency_policy': 'max_opps'}})
        return analysis, result

    def test_selected_feature_profile_changes_pair_prediction_and_keeps_baseline(self):
        compute, memory = self.profile('compute'), self.profile('memory')
        self.assertEqual(set(compute['component_classes'].values()), {'compute'})
        self.assertEqual(set(memory['component_classes'].values()), {'memory'})
        ca, cr = self.search(compute)
        ma, mr = self.search(memory)
        self.assertEqual(ca['baseline'], ma['baseline'])
        self.assertEqual(cr['feature_profile_id'], compute['id'])
        self.assertNotEqual(cr['id'], mr['id'])
        self.assertGreater(mr['best']['metrics']['finish_ms'], cr['best']['metrics']['finish_ms'])
        self.assertGreater(cr['best']['interference']['calibrated_cpu_ms'], 0)
        self.assertEqual(cr['best']['graph']['component_classes'], compute['component_classes'])
        self.assertTrue(all(row['interference']['matched_cells'] for row in cr['candidates']))

    def test_explicit_empty_profile_clears_inheritance_and_reports_fallback(self):
        profile = self.profile('compute')
        analysis, _ = self.search(profile)
        result = self.manager.search({'analysis_id': analysis['id'], 'feature_profile_id': '',
                                     'space': {'worker_counts': [2], 'main_participation': [False], 'frequency_policy': 'max_opps'}})
        self.assertIsNone(result['feature_profile_id'])
        self.assertEqual(result['best']['interference']['calibrated_cpu_ms'], 0)
        self.assertGreater(result['best']['interference']['fallback_cpu_ms'], 0)

    def test_profile_from_another_dataset_is_rejected_before_modeling(self):
        profile = self.profile('compute')
        other = self.manager.import_dataset({'trace_text': two_stage_trace().replace('[First]', '[Another]'), 'hardware_profile_id': HARDWARE_ID})
        with self.assertRaises(PlatformError):
            self.manager.analyze({'dataset_id': other['id'], 'feature_profile_id': profile['id']})

